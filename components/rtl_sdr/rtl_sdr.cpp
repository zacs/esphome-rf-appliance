#include "rtl_sdr.h"

#include <algorithm>
#include <cinttypes>
#include <cstdio>
#include <cstring>

#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <sdkconfig.h>

#include "esphome/core/log.h"

namespace esphome::rtl_sdr {

static const char *const TAG = "rtl_sdr";

// Reader task. It mostly waits on the driver's pull ring; 6 KB leaves room for the driver's error paths.
static constexpr uint32_t READER_STACK = 6144;
// DSP task. Phase 1 only counts; Phase 2's pulse detector keeps its large buffers off the stack.
static constexpr uint32_t DSP_STACK = 8192;
// One driver read waits at most this long, so the reader can notice unplug and error events
static constexpr uint32_t READ_TIMEOUT_MS = 200;
// No samples for this long while streaming means the stream is dead
static constexpr int64_t STALL_US = 3 * 1000 * 1000;
// Consecutive start failures back off 1 s, 2 s, 4 s ... up to this
static constexpr uint32_t START_BACKOFF_MAX_MS = 30000;
// The reader has not reported in for this long: it is stuck inside the driver
static constexpr int64_t STUCK_US = 120LL * 1000 * 1000;

static inline int64_t now_us() { return esp_timer_get_time(); }

static uint64_t counter_delta(uint64_t now, uint64_t last) { return now >= last ? now - last : now; }

void RtlSdrHub::add_band(uint32_t frequency_hz, uint32_t sample_rate, uint32_t dwell_ms) {
  Band band;
  band.frequency_hz = frequency_hz;
  band.sample_rate = sample_rate;
  band.dwell_ms = dwell_ms;
  this->bands_.push_back(band);
}

void RtlSdrHub::setup() {
  if (this->bands_.empty() || this->bands_.size() > MAX_BANDS) {
    this->mark_failed(LOG_STR("bad band list"));
    return;
  }
  for (auto &band : this->bands_) {
    if (!esp_rtl_sdr_quantize_sample_rate(band.sample_rate, &band.exact_rate)) {
      ESP_LOGE(TAG, "The driver cannot program %" PRIu32 " S/s", band.sample_rate);
      this->mark_failed(LOG_STR("unsupported sample rate"));
      return;
    }
    // Whole samples, even byte count; at least one USB packet
    size_t samples = static_cast<size_t>(static_cast<uint64_t>(band.sample_rate) * this->block_ms_ / 1000);
    band.block_bytes = std::max<size_t>(samples * 2, 512);
    this->max_block_bytes_ = std::max(this->max_block_bytes_, band.block_bytes);
  }

  // The pool lives in PSRAM. Blocks are sized for the fastest band; slower bands use the front of each.
  this->blocks_.resize(this->block_count_);
  for (auto &block : this->blocks_) {
    block.data = static_cast<uint8_t *>(heap_caps_malloc(this->max_block_bytes_, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (block.data == nullptr) {
      this->mark_failed(LOG_STR("no PSRAM for the sample blocks"));
      return;
    }
  }
  this->scratch_ = static_cast<uint8_t *>(heap_caps_malloc(this->max_block_bytes_, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  this->free_queue_ = xQueueCreate(this->block_count_, sizeof(uint8_t));
  this->full_queue_ = xQueueCreate(this->block_count_, sizeof(uint8_t));
  this->metrics_lock_ = xSemaphoreCreateMutex();
  if (this->scratch_ == nullptr || this->free_queue_ == nullptr || this->full_queue_ == nullptr ||
      this->metrics_lock_ == nullptr) {
    this->mark_failed(LOG_STR("out of memory"));
    return;
  }
  for (uint8_t i = 0; i < this->block_count_; i++)
    xQueueSend(this->free_queue_, &i, 0);

  // The reader and the driver's USB client task share the ESPHome loop's core; the DSP gets the other one
  this->reader_core_ = xPortGetCoreID();
  if (this->dsp_core_ < 0)
    this->dsp_core_ = this->reader_core_ == 0 ? 1 : 0;

  if (xTaskCreatePinnedToCore(RtlSdrHub::dsp_task_entry_, "rtl_sdr_dsp", DSP_STACK, this, this->dsp_priority_,
                              &this->dsp_task_handle_, this->dsp_core_) != pdPASS ||
      xTaskCreatePinnedToCore(RtlSdrHub::reader_task_entry_, "rtl_sdr_usb", READER_STACK, this,
                              this->reader_priority_, &this->reader_task_handle_, this->reader_core_) != pdPASS) {
    this->mark_failed(LOG_STR("could not start the radio tasks"));
    return;
  }
}

// ---------------------------------------------------------------------------------------------------------
// Reader task
// ---------------------------------------------------------------------------------------------------------

void RtlSdrHub::reader_task_entry_(void *arg) { static_cast<RtlSdrHub *>(arg)->reader_task_(); }

void RtlSdrHub::event_cb_(esp_rtl_sdr_event_t event, const void *payload, void *ctx) {
  // Runs on the driver's USB task. Count only: the reader acts on the counts.
  (void) payload;
  auto *self = static_cast<RtlSdrHub *>(ctx);
  if (event == ESP_RTL_SDR_EVT_DISCONNECTED) {
    self->disconnects_.fetch_add(1, std::memory_order_relaxed);
  } else if (event == ESP_RTL_SDR_EVT_ERROR) {
    self->driver_errors_.fetch_add(1, std::memory_order_relaxed);
  }
}

bool RtlSdrHub::install_driver_() {
  esp_rtl_sdr_config_t config;
  esp_rtl_sdr_config_default(&config);
  config.delivery_mode = ESP_RTL_SDR_DELIVERY_READ;  // the reader pulls; no callback IQ delivery
  config.pull_ring_bytes = this->usb_buffer_;        // 0 = the driver's own choice
  config.usb_task_core_id = static_cast<uint8_t>(this->reader_core_);
  config.event_cb = RtlSdrHub::event_cb_;
  config.event_ctx = this;
  this->transfer_bytes_ = config.transfer_bytes;

  esp_rtl_sdr_handle_t handle = nullptr;
  esp_err_t err = esp_rtl_sdr_install(&config, &handle);
  if (err == ESP_OK) {
    this->handle_ = handle;
    return true;
  }
  if (err == ESP_RTL_SDR_ERR_USB_SAFE_MODE) {
    // The USB stack crashed during enumeration on several boots in a row. The driver keeps USB off for
    // this boot rather than boot-loop. Only a power cycle clears it.
    ESP_LOGE(TAG, "USB host disabled after repeated USB crashes; power-cycle the device to retry");
    this->set_state_(RadioState::USB_DISABLED);
    return false;
  }
  ESP_LOGE(TAG, "Driver install failed: %s", esp_rtl_sdr_err_to_name(err));
  if (handle != nullptr)
    esp_rtl_sdr_uninstall(handle);  // a cleanup-only handle: release what it can
  return false;
}

bool RtlSdrHub::dongle_present_() {
  // Each change is logged once: absence is the normal state of an unplugged port, not news every 500 ms
  esp_rtl_sdr_device_info_t info{};
  if (esp_rtl_sdr_get_device_info(this->handle_, &info) != ESP_OK || !info.present) {
    if (!this->absent_logged_)
      ESP_LOGW(TAG, "No supported RTL-SDR dongle on the USB host port");
    this->absent_logged_ = true;
    this->present_logged_ = false;
    return false;
  }
  if (!this->present_logged_)
    ESP_LOGI(TAG, "Dongle: %s %s, SN %s (%s speed)", info.manufacturer, info.product, info.serial,
             info.high_speed ? "high" : "full");
  this->absent_logged_ = false;
  this->present_logged_ = true;
  return true;
}

bool RtlSdrHub::start_stream_(size_t band_index) {
  const Band &band = this->bands_[band_index];
  if (this->ppm_error_ != 0)
    esp_rtl_sdr_set_freq_correction(this->handle_, this->ppm_error_);
  esp_err_t err = esp_rtl_sdr_start_hz(this->handle_, band.frequency_hz, band.sample_rate);
  if (err == ESP_RTL_SDR_ERR_FAULT) {
    // A failed start or a dead stream leaves the driver in FAULT. stop() drains what is left; reset() clears
    // FAULT once no transfer is live (else ERR_BUSY, and the backoff gives them time).
    esp_rtl_sdr_stop(this->handle_, 0);
    esp_rtl_sdr_reset(this->handle_);
    err = esp_rtl_sdr_start_hz(this->handle_, band.frequency_hz, band.sample_rate);
  }
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Stream start at %" PRIu32 " Hz, %" PRIu32 " S/s failed: %s", band.frequency_hz, band.sample_rate,
             esp_rtl_sdr_err_to_name(err));
    return false;
  }
  // start() zeroed the driver's counters: fold from zero from now on
  xSemaphoreTake(this->metrics_lock_, portMAX_DELAY);
  this->last_metrics_ = {};
  this->metrics_live_ = true;
  xSemaphoreGive(this->metrics_lock_);

  // Gain and bias tee need the claimed interface, so they go after start
  err = this->gain_tenth_db_ < 0 ? esp_rtl_sdr_set_tuner_gain_mode(this->handle_, ESP_RTL_SDR_GAIN_MODE_AUTO)
                                 : esp_rtl_sdr_set_tuner_gain(this->handle_, this->gain_tenth_db_);
  if (err != ESP_OK)
    ESP_LOGW(TAG, "Setting tuner gain failed: %s", esp_rtl_sdr_err_to_name(err));
  // Never power the antenna port: the PoE budget has no room for an active antenna
  esp_rtl_sdr_set_bias_tee(this->handle_, false);

  this->streaming_ = true;
  this->band_ = band_index;
  this->current_band_.store(static_cast<uint8_t>(band_index), std::memory_order_relaxed);
  this->generation_.fetch_add(1, std::memory_order_relaxed);
  this->band_started_us_ = now_us();
  this->band_samples_ = 0;
  this->seen_disconnects_ = this->disconnects_.load();
  this->seen_errors_ = this->driver_errors_.load();
  return true;
}

void RtlSdrHub::stop_stream_() {
  // Fold the old stream's counters before stop: the next start zeroes them
  xSemaphoreTake(this->metrics_lock_, portMAX_DELAY);
  if (this->metrics_live_)
    this->fold_metrics_locked_();
  this->metrics_live_ = false;
  xSemaphoreGive(this->metrics_lock_);
  esp_rtl_sdr_stop(this->handle_, 0);
  this->streaming_ = false;
}

void RtlSdrHub::restart_stream_(const char *why) {
  ESP_LOGW(TAG, "Restarting the stream: %s", why);
  this->stop_stream_();
  this->stream_restarts_.fetch_add(1, std::memory_order_relaxed);
}

void RtlSdrHub::drain_() {
  // A hot retune does not clear the driver's pull ring: what is in it was captured on the old band
  for (int round = 0; round < 64; round++) {
    size_t got = 0;
    if (esp_rtl_sdr_read(this->handle_, this->scratch_, this->max_block_bytes_, 0, &got) != ESP_OK ||
        got < this->max_block_bytes_)
      break;
  }
}

bool RtlSdrHub::hop_to_(size_t next) {
  const Band &from = this->bands_[this->band_];
  const Band &to = this->bands_[next];
  int64_t elapsed_us = now_us() - this->band_started_us_;
  if (elapsed_us > 0) {
    ESP_LOGD(TAG, "%.3f MHz: %.2f MS in %.1f s (%.0f S/s)", from.frequency_hz / 1e6,
             this->band_samples_ / 1e6, elapsed_us / 1e6, this->band_samples_ * 1e6 / elapsed_us);
  }
  this->hops_.fetch_add(1, std::memory_order_relaxed);

  if (from.sample_rate == to.sample_rate) {
    // Same rate: retune in place, no stream restart
    esp_err_t err = esp_rtl_sdr_set_center_freq(this->handle_, to.frequency_hz);
    if (err == ESP_OK) {
      this->drain_();
      this->band_ = next;
      this->current_band_.store(static_cast<uint8_t>(next), std::memory_order_relaxed);
      this->generation_.fetch_add(1, std::memory_order_relaxed);
      this->band_started_us_ = now_us();
      this->band_samples_ = 0;
      return true;
    }
    ESP_LOGW(TAG, "Retune to %" PRIu32 " Hz failed: %s", to.frequency_hz, esp_rtl_sdr_err_to_name(err));
  }
  // A rate change needs stop and start (the driver refuses a rate change while streaming)
  this->stop_stream_();
  return this->start_stream_(next);
}

bool RtlSdrHub::read_block_(size_t band_index) {
  const Band &band = this->bands_[band_index];
  uint8_t index = 0;
  bool pooled = xQueueReceive(this->free_queue_, &index, 0) == pdTRUE;
  uint8_t *dst = pooled ? this->blocks_[index].data : this->scratch_;

  size_t have = 0;
  int64_t last_data_us = now_us();
  while (have < band.block_bytes) {
    if (this->disconnects_.load() != this->seen_disconnects_ || this->driver_errors_.load() != this->seen_errors_) {
      if (pooled)
        xQueueSend(this->free_queue_, &index, 0);
      this->restart_stream_(this->disconnects_.load() != this->seen_disconnects_ ? "dongle disconnected"
                                                                                 : "driver reported an error");
      return false;
    }
    size_t got = 0;
    esp_err_t err = esp_rtl_sdr_read(this->handle_, dst + have, band.block_bytes - have, READ_TIMEOUT_MS, &got);
    int64_t t = now_us();
    this->reader_heartbeat_us_.store(t, std::memory_order_relaxed);
    if (err == ESP_OK) {
      have += got;
      last_data_us = t;
      continue;
    }
    if ((err == ESP_RTL_SDR_ERR_TIMEOUT || err == ESP_ERR_TIMEOUT) && t - last_data_us < STALL_US)
      continue;
    if (pooled)
      xQueueSend(this->free_queue_, &index, 0);
    this->restart_stream_(err == ESP_RTL_SDR_ERR_TIMEOUT || err == ESP_ERR_TIMEOUT ? "no samples for 3 s"
                                                                                  : esp_rtl_sdr_err_to_name(err));
    return false;
  }

  uint64_t samples = have / 2;
  this->samples_read_.fetch_add(samples, std::memory_order_relaxed);
  this->band_samples_ += samples;
  if (!pooled) {
    // The DSP task is behind and holds every block: these samples never reach it
    this->pool_dropped_samples_.fetch_add(samples, std::memory_order_relaxed);
    return true;
  }
  IqBlock &block = this->blocks_[index];
  block.len = static_cast<uint32_t>(have);
  block.band = static_cast<uint8_t>(band_index);
  block.generation = this->generation_.load(std::memory_order_relaxed);
  block.time_us = now_us();
  if (xQueueSend(this->full_queue_, &index, 0) != pdTRUE) {
    // Cannot happen (both queues hold every block), but never wait on the DSP
    xQueueSend(this->free_queue_, &index, 0);
    this->pool_dropped_samples_.fetch_add(samples, std::memory_order_relaxed);
  }
  return true;
}

void RtlSdrHub::reader_task_() {
  this->reader_heartbeat_us_.store(now_us());
  uint32_t install_backoff_ms = 1000;
  while (!this->install_driver_()) {
    if (this->get_state() == RadioState::USB_DISABLED) {
      // Nothing more this boot can do. Park the task; the loop shows the state.
      this->reader_heartbeat_us_.store(INT64_MAX / 2);
      vTaskSuspend(nullptr);
    }
    vTaskDelay(pdMS_TO_TICKS(install_backoff_ms));
    install_backoff_ms = std::min<uint32_t>(install_backoff_ms * 2, START_BACKOFF_MAX_MS);
    this->reader_heartbeat_us_.store(now_us());
  }
  this->set_state_(RadioState::NO_DONGLE);

  int64_t last_hwm_us = 0;
  while (true) {
    int64_t t = now_us();
    this->reader_heartbeat_us_.store(t, std::memory_order_relaxed);
    if (t - last_hwm_us > 5 * 1000 * 1000) {
      last_hwm_us = t;
      this->reader_stack_free_.store(uxTaskGetStackHighWaterMark(nullptr), std::memory_order_relaxed);
    }

    if (!this->streaming_) {
      if (!this->dongle_present_()) {
        this->set_state_(RadioState::NO_DONGLE);
        vTaskDelay(pdMS_TO_TICKS(500));
        continue;
      }
      if (this->start_stream_(this->band_)) {
        this->start_failures_ = 0;
        this->set_state_(RadioState::STREAMING);
      } else {
        this->set_state_(RadioState::FAULT);
        uint32_t backoff_ms = std::min<uint32_t>(1000u << std::min<uint32_t>(this->start_failures_, 5),
                                                 START_BACKOFF_MAX_MS);
        this->start_failures_++;
        vTaskDelay(pdMS_TO_TICKS(backoff_ms));
      }
      continue;
    }

    const Band &band = this->bands_[this->band_];
    if (this->bands_.size() > 1 && band.dwell_ms > 0 &&
        now_us() - this->band_started_us_ >= static_cast<int64_t>(band.dwell_ms) * 1000) {
      size_t next = (this->band_ + 1) % this->bands_.size();
      if (!this->hop_to_(next)) {
        // The start at the new band failed: the outer loop retries there with backoff
        this->band_ = next;
        this->stream_restarts_.fetch_add(1, std::memory_order_relaxed);
        this->set_state_(RadioState::FAULT);
      }
      continue;
    }

    if (!this->read_block_(this->band_))
      this->set_state_(RadioState::FAULT);
  }
}

// ---------------------------------------------------------------------------------------------------------
// DSP task
// ---------------------------------------------------------------------------------------------------------

void RtlSdrHub::dsp_task_entry_(void *arg) { static_cast<RtlSdrHub *>(arg)->dsp_task_(); }

void RtlSdrHub::dsp_task_() {
  int64_t last_hwm_us = 0;
  while (true) {
    uint8_t index = 0;
    if (xQueueReceive(this->full_queue_, &index, pdMS_TO_TICKS(1000)) == pdTRUE) {
      this->process_block_(this->blocks_[index]);
      xQueueSend(this->free_queue_, &index, 0);
    }
    int64_t t = now_us();
    if (t - last_hwm_us > 5 * 1000 * 1000) {
      last_hwm_us = t;
      this->dsp_stack_free_.store(uxTaskGetStackHighWaterMark(nullptr), std::memory_order_relaxed);
    }
  }
}

void RtlSdrHub::process_block_(const IqBlock &block) {
  // Phase 1: count. Phase 2 runs the OOK demodulator and pulse detector on the block here.
  this->samples_processed_.fetch_add(block.len / 2, std::memory_order_relaxed);
}

// ---------------------------------------------------------------------------------------------------------
// ESPHome loop side
// ---------------------------------------------------------------------------------------------------------

void RtlSdrHub::fold_metrics_locked_() {
  esp_rtl_sdr_metrics_t m{};
  if (esp_rtl_sdr_get_metrics(this->handle_, &m) != ESP_OK)
    return;
  this->totals_.overruns += counter_delta(m.overruns, this->last_metrics_.overruns);
  this->totals_.ring_overrun_bytes += counter_delta(m.ring_overrun_bytes, this->last_metrics_.ring_overrun_bytes);
  this->totals_.pull_lock_miss_bytes +=
      counter_delta(m.pull_lock_miss_bytes, this->last_metrics_.pull_lock_miss_bytes);
  this->totals_.slot_starve_blocks += counter_delta(m.slot_starve_blocks, this->last_metrics_.slot_starve_blocks);
  this->effective_sps_ = m.effective_sps;
  this->stream_uptime_ms_ = m.uptime_ms;
  this->last_metrics_ = m;
}

uint64_t RtlSdrHub::dropped_samples_(const DriverTotals &totals) const {
  // Every way a sample can be lost between the dongle and the DSP task, in samples
  return (totals.ring_overrun_bytes + totals.pull_lock_miss_bytes) / 2 +
         totals.slot_starve_blocks * (this->transfer_bytes_ / 2) + this->pool_dropped_samples_.load();
}

void RtlSdrHub::loop() {
  RadioState state = this->get_state();
  if (!this->state_shown_ || state != this->shown_state_) {
    this->show_state_(state);
    this->shown_state_ = state;
    this->state_shown_ = true;
  }
  uint8_t band = this->current_band_.load(std::memory_order_relaxed);
  if (this->band_sensor_ != nullptr && state == RadioState::STREAMING && band != this->published_band_ &&
      band < this->bands_.size()) {
    char text[24];
    snprintf(text, sizeof(text), "%.3f MHz", this->bands_[band].frequency_hz / 1e6);
    this->band_sensor_->publish_state(text);
    this->published_band_ = band;
  }
}

void RtlSdrHub::show_state_(RadioState state) {
  switch (state) {
    case RadioState::STREAMING:
      ESP_LOGI(TAG, "Streaming");
      this->status_clear_error();
      this->status_clear_warning();
      break;
    case RadioState::NO_DONGLE:
      this->status_set_warning(LOG_STR("no RTL-SDR dongle"));
      break;
    case RadioState::FAULT:
      this->status_set_warning(LOG_STR("stream not running, retrying"));
      break;
    case RadioState::USB_DISABLED:
      this->status_set_error(LOG_STR("USB host disabled until power cycle"));
      break;
    case RadioState::STARTING:
      break;
  }
#ifdef USE_RTL_SDR_STATUS_LIGHT
  if (this->status_light_ == nullptr)
    return;
  // Dim and steady: it sits in a box. Green streaming, amber no dongle, red fault, magenta USB off, blue start.
  float r = 0, g = 0, b = 0;
  switch (state) {
    case RadioState::STREAMING:
      g = 1.0f;
      break;
    case RadioState::NO_DONGLE:
      r = 1.0f;
      g = 0.4f;
      break;
    case RadioState::FAULT:
      r = 1.0f;
      break;
    case RadioState::USB_DISABLED:
      r = 1.0f;
      b = 1.0f;
      break;
    case RadioState::STARTING:
      b = 1.0f;
      break;
  }
  auto call = this->status_light_->make_call();
  call.set_state(true);
  call.set_rgb(r, g, b);
  call.set_brightness(0.25f);
  call.set_transition_length(0);
  call.perform();
#endif
}

void RtlSdrHub::update() {
  // Never wait on the reader: if it holds the lock (stopping or starting a stream), use the last totals
  if (xSemaphoreTake(this->metrics_lock_, 0) == pdTRUE) {
    if (this->metrics_live_)
      this->fold_metrics_locked_();
    else
      this->effective_sps_ = this->stream_uptime_ms_ = 0;
    this->shown_totals_ = this->totals_;
    this->shown_sps_ = this->effective_sps_;
    this->shown_uptime_ms_ = this->stream_uptime_ms_;
    xSemaphoreGive(this->metrics_lock_);
  }
  RadioState state = this->get_state();
  uint64_t dropped = this->dropped_samples_(this->shown_totals_);
  uint64_t overruns = this->shown_totals_.overruns;

  // Counts in the log, not samples, and only when they move
  if (dropped != this->last_dropped_logged_ || overruns != this->last_overruns_logged_) {
    ESP_LOGW(TAG, "Lost samples: %" PRIu64 " dropped (+%" PRIu64 "), %" PRIu64 " USB overruns (+%" PRIu64 ")",
             dropped, dropped - this->last_dropped_logged_, overruns, overruns - this->last_overruns_logged_);
    this->last_dropped_logged_ = dropped;
    this->last_overruns_logged_ = overruns;
  }
  ESP_LOGD(TAG, "state %u, band %u, %" PRIu32 " S/s, read %" PRIu64 ", processed %" PRIu64 ", hops %" PRIu32
           ", restarts %" PRIu32,
           static_cast<unsigned>(state), static_cast<unsigned>(this->current_band_.load()), this->shown_sps_,
           this->samples_read_.load(), this->samples_processed_.load(), this->hops_.load(),
           this->stream_restarts_.load());

  // A reader stuck inside a driver call cannot heal itself. Say so.
  bool stuck = now_us() - this->reader_heartbeat_us_.load() > STUCK_US;
  if (stuck && !this->stuck_flagged_) {
    ESP_LOGE(TAG, "The USB reader task has not run for %u s", static_cast<unsigned>(STUCK_US / 1000000));
    this->status_set_error(LOG_STR("USB reader task stuck"));
  } else if (!stuck && this->stuck_flagged_) {
    this->status_clear_error();
  }
  this->stuck_flagged_ = stuck;

  if (this->dongle_sensor_ != nullptr)
    this->dongle_sensor_->publish_state(state == RadioState::STREAMING);
  if (this->dropped_samples_sensor_ != nullptr)
    this->dropped_samples_sensor_->publish_state(static_cast<float>(dropped));
  if (this->usb_overruns_sensor_ != nullptr)
    this->usb_overruns_sensor_->publish_state(static_cast<float>(overruns));
  if (this->stream_restarts_sensor_ != nullptr)
    this->stream_restarts_sensor_->publish_state(static_cast<float>(this->stream_restarts_.load()));
  if (this->samples_sensor_ != nullptr)
    this->samples_sensor_->publish_state(static_cast<float>(this->samples_processed_.load() / 1e6));
  // The driver's figure is bytes since stream start over uptime: it reads low for the first moments after a
  // start (each rate-changing hop is one), so it is published once the stream has run for 5 s
  if (this->effective_sample_rate_sensor_ != nullptr) {
    if (state != RadioState::STREAMING) {
      this->effective_sample_rate_sensor_->publish_state(0);
    } else if (this->shown_uptime_ms_ >= 5000) {
      this->effective_sample_rate_sensor_->publish_state(this->shown_sps_);
    }
  }
  if (this->heap_free_sensor_ != nullptr)
    this->heap_free_sensor_->publish_state(heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
  if (this->psram_free_sensor_ != nullptr)
    this->psram_free_sensor_->publish_state(heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
  // ESP-IDF stack high-water marks are in bytes; 0 means the task has not reported yet
  if (this->reader_stack_free_sensor_ != nullptr && this->reader_stack_free_.load() != 0)
    this->reader_stack_free_sensor_->publish_state(this->reader_stack_free_.load());
  if (this->dsp_stack_free_sensor_ != nullptr && this->dsp_stack_free_.load() != 0)
    this->dsp_stack_free_sensor_->publish_state(this->dsp_stack_free_.load());
  this->publish_cpu_load_();
}

void RtlSdrHub::publish_cpu_load_() {
#if defined(USE_RTL_SDR_CPU_LOAD) && configGENERATE_RUN_TIME_STATS
  // Run-time counters tick in microseconds: load = 1 - idle time / wall time, per core
  int64_t t = now_us();
  int64_t elapsed = t - this->last_cpu_time_us_;
  for (int core = 0; core < 2; core++) {
    uint32_t idle = static_cast<uint32_t>(ulTaskGetIdleRunTimeCounterForCore(core));
    uint32_t idle_delta = idle - this->last_idle_[core];
    this->last_idle_[core] = idle;
    if (this->last_cpu_time_us_ != 0 && elapsed > 0 && this->cpu_load_sensor_[core] != nullptr) {
      float load = 100.0f * (1.0f - static_cast<float>(idle_delta) / static_cast<float>(elapsed));
      this->cpu_load_sensor_[core]->publish_state(std::clamp(load, 0.0f, 100.0f));
    }
  }
  this->last_cpu_time_us_ = t;
#endif
}

void RtlSdrHub::dump_config() {
  ESP_LOGCONFIG(TAG, "RTL-SDR hub (radio frequency remote adapter):");
  for (size_t i = 0; i < this->bands_.size(); i++) {
    const Band &b = this->bands_[i];
    ESP_LOGCONFIG(TAG,
                  "  Band %u: %.3f MHz at %" PRIu32 " S/s (exact %" PRIu32 "), hears %.3f-%.3f MHz, dwell %" PRIu32
                  " ms, block %u B",
                  static_cast<unsigned>(i), b.frequency_hz / 1e6, b.sample_rate, b.exact_rate,
                  (b.frequency_hz - b.sample_rate / 2) / 1e6, (b.frequency_hz + b.sample_rate / 2) / 1e6, b.dwell_ms,
                  static_cast<unsigned>(b.block_bytes));
  }
  if (this->gain_tenth_db_ < 0) {
    ESP_LOGCONFIG(TAG, "  Gain: tuner AGC");
  } else {
    ESP_LOGCONFIG(TAG, "  Gain: %.1f dB", this->gain_tenth_db_ / 10.0f);
  }
  const char *if_mode = "vendor_fixed";
#if defined(CONFIG_ESP_RTL_SDR_ESP_R820T2_IF_TABLE)
  if_mode = "table";
#elif defined(CONFIG_ESP_RTL_SDR_ESP_R820T2_IF_LIBRTLSDR)
  if_mode = "librtlsdr";
#endif
  ESP_LOGCONFIG(TAG,
                "  PPM error: %d\n"
                "  R820T2 IF mode: %s\n"
                "  Bias tee: off (always)\n"
                "  USB pull ring: %" PRIu32 " B (0 = driver default)\n"
                "  Block pool: %u x %u B in PSRAM\n"
                "  Reader task: core %d, priority %u\n"
                "  DSP task: core %d, priority %u",
                this->ppm_error_, if_mode, this->usb_buffer_, static_cast<unsigned>(this->block_count_),
                static_cast<unsigned>(this->max_block_bytes_), this->reader_core_,
                static_cast<unsigned>(this->reader_priority_), this->dsp_core_,
                static_cast<unsigned>(this->dsp_priority_));
  LOG_UPDATE_INTERVAL(this);
  LOG_BINARY_SENSOR("  ", "Dongle", this->dongle_sensor_);
  LOG_TEXT_SENSOR("  ", "Band", this->band_sensor_);
  LOG_SENSOR("  ", "Dropped Samples", this->dropped_samples_sensor_);
  LOG_SENSOR("  ", "USB Overruns", this->usb_overruns_sensor_);
  LOG_SENSOR("  ", "Stream Restarts", this->stream_restarts_sensor_);
  LOG_SENSOR("  ", "Samples", this->samples_sensor_);
  LOG_SENSOR("  ", "Effective Sample Rate", this->effective_sample_rate_sensor_);
  LOG_SENSOR("  ", "Heap Free", this->heap_free_sensor_);
  LOG_SENSOR("  ", "PSRAM Free", this->psram_free_sensor_);
  LOG_SENSOR("  ", "Reader Stack Free", this->reader_stack_free_sensor_);
  LOG_SENSOR("  ", "DSP Stack Free", this->dsp_stack_free_sensor_);
  LOG_SENSOR("  ", "CPU Load Core 0", this->cpu_load_sensor_[0]);
  LOG_SENSOR("  ", "CPU Load Core 1", this->cpu_load_sensor_[1]);
}

}  // namespace esphome::rtl_sdr
