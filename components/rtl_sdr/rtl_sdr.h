#pragma once

// rtl_sdr hub: an RTL-SDR dongle on the ESP32-P4 USB host port, driven by esp_rtl_sdr.
//
// Two FreeRTOS tasks do the real-time work. Neither ever runs in the ESPHome loop.
//  - The reader task owns the driver. It installs it, starts and restarts the stream, hops between
//    bands, and copies CU8 samples from the driver's pull ring into fixed-size blocks.
//  - The DSP task takes full blocks, processes them, and gives them back. In Phase 1 it only
//    counts samples. Phase 2 puts the OOK demodulator and pulse detector here.
// Blocks go back and forth through two bounded FreeRTOS queues. If the DSP task falls behind, the
// reader drops the block and counts it; it never waits for the DSP.

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <vector>

#include "esphome/core/component.h"
#include "esphome/core/defines.h"
#include "esphome/components/binary_sensor/binary_sensor.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/text_sensor/text_sensor.h"
#ifdef USE_RTL_SDR_STATUS_LIGHT
#include "esphome/components/light/light_state.h"
#endif

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include "esp_rtl_sdr.h"

namespace esphome::rtl_sdr {

static constexpr size_t MAX_BANDS = 4;

/// One configured band.
struct Band {
  uint32_t frequency_hz{0};
  uint32_t sample_rate{0};  ///< as configured
  uint32_t exact_rate{0};   ///< what the RTL2832U resampler realises (driver quantizer); use this for timing
  uint32_t dwell_ms{0};     ///< 0 with a single band: never hop
  size_t block_bytes{0};    ///< one hand-off at this rate (CU8, 2 bytes per sample)
};

/// One hand-off from the reader task to the DSP task. A block never straddles a hop or a restart.
struct IqBlock {
  uint8_t *data{nullptr};
  uint32_t len{0};         ///< bytes of CU8
  uint8_t band{0};         ///< index into the band list
  uint32_t generation{0};  ///< bumped on every stream start and hop: DSP state must reset when it changes
  int64_t time_us{0};      ///< esp_timer time the block was complete
};

enum class RadioState : uint8_t {
  STARTING = 0,      ///< driver not installed yet
  NO_DONGLE = 1,     ///< driver up, no supported dongle attached
  STREAMING = 2,     ///< samples flowing
  FAULT = 3,         ///< dongle attached, stream will not start; backing off and retrying
  USB_DISABLED = 4,  ///< the driver's crash guard latched: USB host off until a power cycle
};

/// Cumulative counters, folded from the driver's per-stream metrics (they restart with every stream start).
struct DriverTotals {
  uint64_t overruns{0};
  uint64_t ring_overrun_bytes{0};
  uint64_t pull_lock_miss_bytes{0};
  uint64_t slot_starve_blocks{0};
};

class RtlSdrHub : public PollingComponent {
 public:
  void setup() override;
  void loop() override;
  void update() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

  // Configuration (from codegen)
  void add_band(uint32_t frequency_hz, uint32_t sample_rate, uint32_t dwell_ms);
  void set_gain_tenth_db(int gain) { this->gain_tenth_db_ = gain; }
  void set_ppm_error(int ppm) { this->ppm_error_ = ppm; }
  void set_usb_buffer(uint32_t bytes) { this->usb_buffer_ = bytes; }
  void set_block_ms(uint32_t ms) { this->block_ms_ = ms; }
  void set_block_count(uint8_t count) { this->block_count_ = count; }
  void set_dsp_core(int core) { this->dsp_core_ = core; }
  void set_reader_priority(uint8_t priority) { this->reader_priority_ = priority; }
  void set_dsp_priority(uint8_t priority) { this->dsp_priority_ = priority; }
#ifdef USE_RTL_SDR_STATUS_LIGHT
  void set_status_light(light::LightState *light) { this->status_light_ = light; }
#endif

  void set_dongle_binary_sensor(binary_sensor::BinarySensor *s) { this->dongle_sensor_ = s; }
  void set_band_text_sensor(text_sensor::TextSensor *s) { this->band_sensor_ = s; }
  void set_dropped_samples_sensor(sensor::Sensor *s) { this->dropped_samples_sensor_ = s; }
  void set_usb_overruns_sensor(sensor::Sensor *s) { this->usb_overruns_sensor_ = s; }
  void set_stream_restarts_sensor(sensor::Sensor *s) { this->stream_restarts_sensor_ = s; }
  void set_samples_sensor(sensor::Sensor *s) { this->samples_sensor_ = s; }
  void set_effective_sample_rate_sensor(sensor::Sensor *s) { this->effective_sample_rate_sensor_ = s; }
  void set_heap_free_sensor(sensor::Sensor *s) { this->heap_free_sensor_ = s; }
  void set_psram_free_sensor(sensor::Sensor *s) { this->psram_free_sensor_ = s; }
  void set_reader_stack_free_sensor(sensor::Sensor *s) { this->reader_stack_free_sensor_ = s; }
  void set_dsp_stack_free_sensor(sensor::Sensor *s) { this->dsp_stack_free_sensor_ = s; }
  void set_cpu_load_core0_sensor(sensor::Sensor *s) { this->cpu_load_sensor_[0] = s; }
  void set_cpu_load_core1_sensor(sensor::Sensor *s) { this->cpu_load_sensor_[1] = s; }

  // For the platforms built on the hub
  const std::vector<Band> &get_bands() const { return this->bands_; }
  RadioState get_state() const { return this->state_.load(std::memory_order_relaxed); }

 protected:
  // Reader task: owns the driver handle. Nothing else calls start/stop/retune/reset.
  static void reader_task_entry_(void *arg);
  void reader_task_();
  bool install_driver_();
  bool dongle_present_();
  bool start_stream_(size_t band);
  void stop_stream_();
  bool hop_to_(size_t band);
  void drain_();
  bool read_block_(size_t band);
  void restart_stream_(const char *why);

  // DSP task
  static void dsp_task_entry_(void *arg);
  void dsp_task_();
  void process_block_(const IqBlock &block);

  // Driver events arrive on the driver's USB task: count them, act on them in the reader
  static void event_cb_(esp_rtl_sdr_event_t event, const void *payload, void *ctx);

  void fold_metrics_locked_();
  void set_state_(RadioState state) { this->state_.store(state, std::memory_order_relaxed); }
  uint64_t dropped_samples_(const DriverTotals &totals) const;
  void publish_cpu_load_();
  void show_state_(RadioState state);

  // Configuration
  std::vector<Band> bands_;
  int gain_tenth_db_{-1};  // < 0: tuner AGC
  int ppm_error_{0};
  uint32_t usb_buffer_{0};
  uint32_t block_ms_{32};
  uint8_t block_count_{8};
  int dsp_core_{-1};
  int reader_core_{0};
  uint8_t reader_priority_{6};
  uint8_t dsp_priority_{5};

  // Driver (reader task only, apart from the thread-safe getters)
  esp_rtl_sdr_handle_t handle_{nullptr};
  size_t transfer_bytes_{0};
  bool streaming_{false};
  size_t band_{0};
  int64_t band_started_us_{0};
  uint64_t band_samples_{0};
  uint32_t start_failures_{0};
  uint32_t seen_disconnects_{0};
  uint32_t seen_errors_{0};
  bool absent_logged_{false};
  bool present_logged_{false};

  // Block pool: indices travel through the queues, the blocks stay put
  std::vector<IqBlock> blocks_;
  uint8_t *scratch_{nullptr};  // where a block goes when the pool is empty (it is then dropped)
  size_t max_block_bytes_{0};
  QueueHandle_t free_queue_{nullptr};
  QueueHandle_t full_queue_{nullptr};
  TaskHandle_t reader_task_handle_{nullptr};
  TaskHandle_t dsp_task_handle_{nullptr};

  // Shared counters (written by the tasks, read by the loop)
  std::atomic<RadioState> state_{RadioState::STARTING};
  std::atomic<uint8_t> current_band_{0};
  std::atomic<uint32_t> generation_{0};
  std::atomic<uint32_t> disconnects_{0};
  std::atomic<uint32_t> driver_errors_{0};
  std::atomic<uint32_t> stream_restarts_{0};
  std::atomic<uint32_t> hops_{0};
  std::atomic<uint64_t> samples_read_{0};
  std::atomic<uint64_t> samples_processed_{0};
  std::atomic<uint64_t> pool_dropped_samples_{0};
  std::atomic<uint32_t> reader_stack_free_{0};
  std::atomic<uint32_t> dsp_stack_free_{0};
  std::atomic<int64_t> reader_heartbeat_us_{0};

  // Driver metrics, folded under metrics_lock_. metrics_live_ is false between a stop and the next start,
  // when the driver still holds the old stream's numbers.
  SemaphoreHandle_t metrics_lock_{nullptr};
  bool metrics_live_{false};
  esp_rtl_sdr_metrics_t last_metrics_{};
  DriverTotals totals_{};
  uint32_t effective_sps_{0};
  uint32_t stream_uptime_ms_{0};
  // The loop's copy, taken under the lock (64-bit values could tear otherwise)
  DriverTotals shown_totals_{};
  uint32_t shown_sps_{0};
  uint32_t shown_uptime_ms_{0};

  // Loop-side bookkeeping
  RadioState shown_state_{RadioState::STARTING};
  bool state_shown_{false};
  uint8_t published_band_{0xFF};
  uint64_t last_dropped_logged_{0};
  uint64_t last_overruns_logged_{0};
  bool stuck_flagged_{false};
#ifdef USE_RTL_SDR_CPU_LOAD
  int64_t last_cpu_time_us_{0};
  uint32_t last_idle_[2]{0, 0};
#endif

#ifdef USE_RTL_SDR_STATUS_LIGHT
  light::LightState *status_light_{nullptr};
#endif
  binary_sensor::BinarySensor *dongle_sensor_{nullptr};
  text_sensor::TextSensor *band_sensor_{nullptr};
  sensor::Sensor *dropped_samples_sensor_{nullptr};
  sensor::Sensor *usb_overruns_sensor_{nullptr};
  sensor::Sensor *stream_restarts_sensor_{nullptr};
  sensor::Sensor *samples_sensor_{nullptr};
  sensor::Sensor *effective_sample_rate_sensor_{nullptr};
  sensor::Sensor *heap_free_sensor_{nullptr};
  sensor::Sensor *psram_free_sensor_{nullptr};
  sensor::Sensor *reader_stack_free_sensor_{nullptr};
  sensor::Sensor *dsp_stack_free_sensor_{nullptr};
  sensor::Sensor *cpu_load_sensor_[2]{nullptr, nullptr};
};

}  // namespace esphome::rtl_sdr
