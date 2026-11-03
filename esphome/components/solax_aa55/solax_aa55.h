#pragma once
#include "esphome/core/component.h"
#include "esphome/components/uart/uart.h"
#include "esphome/components/socket/socket.h"
#include <deque>
#include <memory>
#include <string>
#include <vector>

namespace esphome {
namespace solax_aa55 {

// SolaX "Pocket" dongle replacement: speaks the AA55 Pocket-USB protocol to the
// inverter on the dongle UART and serves Modbus TCP by tunnelling each request
// in an AA55 ctrl-0x14 frame, exactly like the stock Pocket WiFi 3.010.16 does
// (reverse-engineered), but non-blocking, multi-client and without stock's
// 650 ms frame spacing once the link is at 115200.
//
// Link bring-up (stock behaviour): send `01 05` once a second until the inverter
// answers with `01 85`; register the dongle serial once (`02 01 <SN>`); if the
// 0x85 reply's byte [0x2E] == 1, ACK with `01 85`, wait 1 s and move to 115200.
// After 30 s of silence toggle the baud rate and redo the handshake. Re-register
// if the inverter sends `02 41`.
class SolaxAA55 : public Component, public uart::UARTDevice {
 public:
  void set_dongle_serial(const std::string &s) { this->dongle_serial_ = s; }
  void set_modbus_port(uint16_t p) { this->modbus_port_ = p; }
  void set_response_timeout(uint32_t ms) { this->response_timeout_ = ms; }
  void set_frame_gap(uint32_t fast_ms, uint32_t slow_ms) { this->frame_gap_fast_ = fast_ms; this->frame_gap_slow_ = slow_ms; }
  void set_allow_115200(bool v) { this->allow_115200_ = v; }
  void set_keepalive_interval(uint32_t ms) { this->keepalive_interval_ = ms; }
  void set_poll_interval(uint32_t ms) { this->poll_interval_ = ms; }
  void set_silence_timeout(uint32_t ms) { this->silence_timeout_ = ms; }
  void set_max_clients(uint8_t n) { this->max_clients_ = n; }

  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  // Read-only state for template sensors
  bool linked() const { return this->state_ == LinkState::ONLINE; }
  bool registered() const { return this->registered_; }
  const std::string &inverter_serial() const { return this->inverter_serial_; }
  uint8_t inverter_type() const { return this->inverter_type_; }
  uint16_t model_code() const { return this->model_code_; }
  uint32_t link_baud() const { return this->current_baud_; }
  uint32_t modbus_ok_count() const { return this->stat_ok_; }
  uint32_t modbus_timeout_count() const { return this->stat_timeout_; }

 protected:
  enum class LinkState : uint8_t { HANDSHAKE, ACK_WAIT, ONLINE };

  struct Client {
    std::unique_ptr<socket::Socket> sock;
    std::vector<uint8_t> rx;
    uint32_t last_activity{0};
  };
  struct Request {
    uint8_t client;             // index into clients_
    std::vector<uint8_t> tcp;   // full MBAP + unit + PDU as received
    uint32_t queued_at{0};
    bool retried{false};        // one silent retry on a missed inverter reply
  };

  // UART side
  void drain_uart_();
  void parse_frames_();
  void handle_frame_(uint8_t ctrl, uint8_t func, const uint8_t *raw, size_t len);
  void send_frame_(uint8_t ctrl, uint8_t func, const uint8_t *payload, size_t plen);
  void send_raw_(const uint8_t *data, size_t len);
  bool can_send_() const;
  void set_baud_(uint32_t baud);
  static uint16_t sum16_(const uint8_t *d, size_t n);
  static uint16_t modbus_crc_(const uint8_t *d, size_t n);

  // link
  void run_link_();
  void go_handshake_(const char *why);

  // Modbus TCP side
  void accept_clients_();
  void service_clients_();
  void run_modbus_();
  void send_modbus_exception_(uint8_t client, const std::vector<uint8_t> &tcp, uint8_t code);
  void deliver_(uint8_t client, const uint8_t *data, size_t len);
  void send_tunnelled_(const std::vector<uint8_t> &tcp);

  // config
  std::string dongle_serial_;
  uint16_t modbus_port_{502};
  uint32_t response_timeout_{1000};
  uint32_t frame_gap_fast_{100};
  uint32_t frame_gap_slow_{650};
  bool allow_115200_{true};
  uint32_t keepalive_interval_{10000};
  uint32_t poll_interval_{5000};      // stock polls realtime every 5 s; the inverter drops to 9600 after ~30 s without a ctrl-01 query
  uint32_t last_poll_tx_{0};
  uint32_t poll_pending_until_{0};
  uint8_t realtime_func_{0x10};       // type-15 realtime query (stock code table; 0x0C for older types)
  uint32_t silence_timeout_{30000};
  uint8_t max_clients_{4};

  // link state
  LinkState state_{LinkState::HANDSHAKE};
  uint32_t current_baud_{9600};
  bool registered_{false};
  bool register_sent_{false};
  uint8_t reregister_left_{5};
  bool want_115200_{false};
  uint32_t baud_switch_at_{0};
  uint32_t last_tx_{0};
  uint32_t last_rx_{0};
  uint32_t last_handshake_tx_{0};
  uint8_t silence_toggles_{0};
  std::string inverter_serial_;
  uint8_t inverter_type_{0};
  uint16_t model_code_{0};

  // UART rx
  std::vector<uint8_t> ubuf_;

  // Modbus
  std::unique_ptr<socket::Socket> listen_;
  std::vector<Client> clients_;
  std::deque<Request> queue_;
  bool inflight_{false};
  Request inflight_req_;
  uint32_t inflight_deadline_{0};
  uint8_t consecutive_timeouts_{0};
  uint32_t stat_ok_{0};
  uint32_t stat_timeout_{0};
  uint32_t stat_retry_{0};

 public:
  uint32_t modbus_retry_count() const { return this->stat_retry_; }
};

}  // namespace solax_aa55
}  // namespace esphome
