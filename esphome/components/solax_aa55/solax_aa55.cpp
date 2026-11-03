#include "solax_aa55.h"
#include "esphome/core/log.h"
#include "esphome/core/helpers.h"
#include <cerrno>
#include <cstring>

namespace esphome {
namespace solax_aa55 {

static const char *const TAG = "solax_aa55";

static const uint8_t CTRL_QUERY = 0x01;
static const uint8_t CTRL_REGISTER = 0x02;
static const uint8_t CTRL_MODBUS = 0x14;
static const uint8_t FUNC_REGISTER = 0x01;
static const uint8_t FUNC_SERIAL = 0x05;
static const uint8_t FUNC_SERIAL_RESP = 0x85;
static const uint8_t FUNC_REREGISTER = 0x41;
static const uint8_t MODBUS_SLOT = 0x01;  // stock uses its client slot (1/2); the inverter echoes it

static const size_t MAX_FRAME = 1100;     // settings replies can exceed 255 bytes (LEN extension)
static const uint32_t MAX_QUEUE = 8;
static const uint32_t CLIENT_IDLE_MS = 90000;

uint16_t SolaxAA55::sum16_(const uint8_t *d, size_t n) {
  uint32_t s = 0;
  for (size_t i = 0; i < n; i++) s += d[i];
  return s & 0xFFFF;
}

uint16_t SolaxAA55::modbus_crc_(const uint8_t *d, size_t n) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < n; i++) {
    crc ^= d[i];
    for (int b = 0; b < 8; b++) crc = (crc & 1) ? (crc >> 1) ^ 0xA001 : (crc >> 1);
  }
  return crc;
}

void SolaxAA55::setup() {
  this->listen_ = socket::socket_ip(SOCK_STREAM, 0);
  if (!this->listen_) { ESP_LOGE(TAG, "socket failed"); this->mark_failed(); return; }
  int enable = 1;
  this->listen_->setsockopt(SOL_SOCKET, SO_REUSEADDR, &enable, sizeof(enable));
  this->listen_->setblocking(false);
  struct sockaddr_storage ss;
  socklen_t sl = socket::set_sockaddr_any(reinterpret_cast<struct sockaddr *>(&ss), sizeof(ss), this->modbus_port_);
  if (this->listen_->bind(reinterpret_cast<struct sockaddr *>(&ss), sl) != 0) { ESP_LOGE(TAG, "bind :%u failed", this->modbus_port_); this->mark_failed(); return; }
  if (this->listen_->listen(4) != 0) { ESP_LOGE(TAG, "listen failed"); this->mark_failed(); return; }
  this->current_baud_ = this->parent_->get_baud_rate();
  this->last_rx_ = millis();
  ESP_LOGCONFIG(TAG, "Modbus TCP on :%u, inverter link starting at %u baud", this->modbus_port_, this->current_baud_);
}

void SolaxAA55::dump_config() {
  ESP_LOGCONFIG(TAG, "SolaX AA55 dongle: serial=%s modbus_port=%u timeout=%ums gap=%u/%ums allow_115200=%s",
                this->dongle_serial_.c_str(), this->modbus_port_, this->response_timeout_,
                this->frame_gap_fast_, this->frame_gap_slow_, YESNO(this->allow_115200_));
}

// ---------------------------------------------------------------- UART side

bool SolaxAA55::can_send_() const {
  uint32_t gap = (this->current_baud_ >= 115200) ? this->frame_gap_fast_ : this->frame_gap_slow_;
  return (millis() - this->last_tx_) >= gap;
}

void SolaxAA55::send_raw_(const uint8_t *data, size_t len) {
  this->write_array(data, len);
  this->last_tx_ = millis();
  ESP_LOGV(TAG, "TX %s", format_hex_pretty(data, len).c_str());
}

void SolaxAA55::send_frame_(uint8_t ctrl, uint8_t func, const uint8_t *payload, size_t plen) {
  std::vector<uint8_t> f;
  f.reserve(plen + 7);
  f.push_back(0xAA); f.push_back(0x55); f.push_back((uint8_t) (plen + 7)); f.push_back(ctrl); f.push_back(func);
  if (plen) f.insert(f.end(), payload, payload + plen);
  uint16_t s = sum16_(f.data(), f.size());
  f.push_back(s & 0xFF); f.push_back(s >> 8);
  this->send_raw_(f.data(), f.size());
}

void SolaxAA55::set_baud_(uint32_t baud) {
  if (baud == this->current_baud_) return;
  this->parent_->set_baud_rate(baud);
  this->parent_->load_settings(false);
  this->current_baud_ = baud;
  this->ubuf_.clear();
  ESP_LOGI(TAG, "UART now %u baud", baud);
}

void SolaxAA55::drain_uart_() {
  uint8_t b;
  int n = 0;
  while (this->available() && n < 512) {
    if (!this->read_byte(&b)) break;
    this->ubuf_.push_back(b);
    n++;
  }
  if (this->ubuf_.size() > 4096) this->ubuf_.erase(this->ubuf_.begin(), this->ubuf_.end() - 2048);
}

void SolaxAA55::parse_frames_() {
  auto &u = this->ubuf_;
  while (u.size() >= 3) {
    // resync to a frame head
    size_t i = 0;
    while (i + 1 < u.size() && !((u[i] == 0xAA || u[i] == 0xBB) && u[i + 1] == 0x55)) i++;
    if (i) u.erase(u.begin(), u.begin() + i);
    if (u.size() < 4) return;
    if (u[0] == 0xBB) {                       // long "unlimited transfer" frame: BB 55 LENlo LENhi ...
      size_t len = u[2] | (u[3] << 8);
      if (len < 7 || len > MAX_FRAME) { u.erase(u.begin(), u.begin() + 2); continue; }
      if (u.size() < len) return;
      ESP_LOGD(TAG, "long frame func=0x%02X len=%u ignored", u[4], (unsigned) len);
      u.erase(u.begin(), u.begin() + len);
      continue;
    }
    size_t len = u[2];
    uint8_t ctrl = u[3];
    len |= (ctrl & 0xC0) << 2;                 // stock: ctrl bits 7:6 extend LEN bits 9:8
    ctrl &= 0x3F;
    if (len == 0x97 && (u[3] & 0xC0) == 0) len = 0x197;  // stock quirk: the 407-byte realtime/settings replies carry LEN 0x97
    if (len < 7 || len > MAX_FRAME) { u.erase(u.begin(), u.begin() + 2); continue; }
    if (u.size() < len) return;                // wait for the rest
    if (u.size() < 5) return;
    uint8_t func = u[4];
    uint16_t want = u[len - 2] | (u[len - 1] << 8);
    bool sum_ok = (sum16_(u.data(), len - 2) == want);
    bool reg_echo = (ctrl == CTRL_REGISTER && func == FUNC_REGISTER);  // some inverters omit the sum here
    if (!sum_ok && !reg_echo) {
      ESP_LOGW(TAG, "checksum error ctrl=%02X func=%02X len=%u", ctrl, func, (unsigned) len);
      u.erase(u.begin(), u.begin() + 2);
      continue;
    }
    this->last_rx_ = millis();
    this->silence_toggles_ = 0;
    this->handle_frame_(ctrl, func, u.data(), len);
    u.erase(u.begin(), u.begin() + len);
  }
}

void SolaxAA55::handle_frame_(uint8_t ctrl, uint8_t func, const uint8_t *raw, size_t len) {
  ESP_LOGV(TAG, "RX ctrl=%02X func=%02X len=%u", ctrl, func, (unsigned) len);
  if (ctrl == CTRL_QUERY && func == FUNC_SERIAL_RESP && len >= 0x2C) {
    std::string sn(reinterpret_cast<const char *>(raw + 5), 14);
    while (!sn.empty() && (sn.back() == ' ' || sn.back() == 0)) sn.pop_back();
    this->inverter_serial_ = sn;
    this->model_code_ = raw[0x21] | (raw[0x22] << 8);
    this->inverter_type_ = raw[0x2B];
    bool want_fast = (len >= 0x30) && (raw[0x2E] == 1);
    if (this->state_ == LinkState::HANDSHAKE) {
      ESP_LOGI(TAG, "inverter %s type=%u model=0x%04X wants115200=%s", sn.c_str(), this->inverter_type_, this->model_code_, YESNO(want_fast));
      if (!this->register_sent_) {
        this->send_frame_(CTRL_REGISTER, FUNC_REGISTER, reinterpret_cast<const uint8_t *>(this->dongle_serial_.data()), 10);
        this->register_sent_ = true;
      }
      this->want_115200_ = want_fast;
      if (want_fast && this->allow_115200_ && this->current_baud_ != 115200) {
        this->state_ = LinkState::ACK_WAIT;   // ACK is sent by run_link_ once the frame gap allows
        this->baud_switch_at_ = 0;
      } else {
        this->state_ = LinkState::ONLINE;
        ESP_LOGI(TAG, "link ONLINE at %u baud", this->current_baud_);
      }
    }
    return;
  }
  if (ctrl == CTRL_REGISTER && func == FUNC_REGISTER) {
    if (!this->registered_) ESP_LOGI(TAG, "dongle registered with inverter");
    this->registered_ = true;
    return;
  }
  if (ctrl == CTRL_REGISTER && func == FUNC_REREGISTER) {
    if (this->reregister_left_) {
      this->reregister_left_--;
      ESP_LOGW(TAG, "inverter asked us to re-register (%u left)", this->reregister_left_);
      this->register_sent_ = false;
      this->registered_ = false;
      this->send_frame_(CTRL_REGISTER, FUNC_REGISTER, reinterpret_cast<const uint8_t *>(this->dongle_serial_.data()), 10);
      this->register_sent_ = true;
    }
    return;
  }
  if (ctrl == CTRL_MODBUS) {
    // raw = AA 55 LEN 14 <slot> <MBAP(6) unit PDU...> CRC(2) SUM(2)  -- the slot sits in the func byte
    if (!this->inflight_) { ESP_LOGD(TAG, "unsolicited modbus frame ignored"); return; }
    if (len < 5 + 7 + 2 + 2) { ESP_LOGW(TAG, "modbus frame too short (%u)", (unsigned) len); return; }
    const uint8_t *tcp = raw + 5;
    size_t tcp_len = len - 5 - 2 - 2;        // minus header(incl. slot), CRC, SUM
    uint16_t crc_want = tcp[tcp_len] | (tcp[tcp_len + 1] << 8);
    uint16_t crc_got = modbus_crc_(tcp + 6, tcp_len - 6);
    if (crc_want != crc_got) ESP_LOGW(TAG, "modbus reply CRC mismatch (%04X vs %04X), forwarding anyway", crc_want, crc_got);
    uint16_t tid = (tcp[0] << 8) | tcp[1];
    uint16_t my_tid = (this->inflight_req_.tcp[0] << 8) | this->inflight_req_.tcp[1];
    if (tid != my_tid) { ESP_LOGW(TAG, "modbus reply tid %u != inflight %u, dropped", tid, my_tid); return; }
    std::vector<uint8_t> out(tcp, tcp + tcp_len);
    uint16_t mlen = tcp_len - 6;
    out[4] = mlen >> 8; out[5] = mlen & 0xFF;  // stock also rewrites the MBAP length
    this->deliver_(this->inflight_req_.client, out.data(), out.size());
    this->inflight_ = false;
    this->consecutive_timeouts_ = 0;
    this->stat_ok_++;
    return;
  }
  if (ctrl == CTRL_QUERY && func == (uint8_t) (this->realtime_func_ | 0x80)) {
    this->poll_pending_until_ = 0;   // realtime reply landed; Modbus may proceed
    return;
  }
  // everything else (settings, time sync replies...) just proves the link is alive
}

// ---------------------------------------------------------------- link

void SolaxAA55::go_handshake_(const char *why) {
  ESP_LOGW(TAG, "link down (%s); re-handshaking", why);
  this->state_ = LinkState::HANDSHAKE;
  this->last_handshake_tx_ = 0;
  if (this->inflight_) {
    this->send_modbus_exception_(this->inflight_req_.client, this->inflight_req_.tcp, 0x0B);
    this->inflight_ = false;
  }
}

void SolaxAA55::run_link_() {
  uint32_t now = millis();
  switch (this->state_) {
    case LinkState::HANDSHAKE:
      if (now - this->last_handshake_tx_ >= 1000 && this->can_send_()) {
        this->send_frame_(CTRL_QUERY, FUNC_SERIAL, nullptr, 0);
        this->last_handshake_tx_ = now;
      }
      // while handshaking, try the other baud rate every 5 s (the inverter stays at
      // 115200 once switched, so after a dongle reboot 9600 gets no answer)
      if (now - this->last_rx_ > 5000) {
        this->last_rx_ = now;
        if (this->allow_115200_) {
          this->set_baud_(this->current_baud_ == 9600 ? 115200 : 9600);
          this->silence_toggles_++;
        }
      }
      break;
    case LinkState::ACK_WAIT:
      if (this->baud_switch_at_ == 0) {
        if (this->can_send_()) {
          this->send_frame_(CTRL_QUERY, FUNC_SERIAL_RESP, nullptr, 0);  // AA 55 07 01 85 8C 01
          this->baud_switch_at_ = now + 1000;
          ESP_LOGI(TAG, "ACKed 115200 request, switching in 1 s");
        }
      } else if ((int32_t) (now - this->baud_switch_at_) >= 0) {
        this->set_baud_(115200);
        this->state_ = LinkState::ONLINE;
        this->last_rx_ = now;
        ESP_LOGI(TAG, "link ONLINE at 115200 baud");
      }
      break;
    case LinkState::ONLINE:
      if (now - this->last_rx_ > this->silence_timeout_) {
        // stock toggles 9600/115200 and redoes the 01 05 handshake
        if (this->allow_115200_) this->set_baud_(this->current_baud_ == 9600 ? 115200 : 9600);
        this->go_handshake_("silence");
        this->last_rx_ = now;
        break;
      }
      // Periodic realtime poll like stock (every 5 s), with priority over queued Modbus requests:
      // the inverter treats the link as dead and falls back to 9600 after ~30 s without a
      // ctrl-01 query, and tunnelled Modbus frames do NOT count as activity.
      if (!this->inflight_ && (now - this->last_poll_tx_) >= this->poll_interval_ && this->can_send_()) {
        this->send_frame_(CTRL_QUERY, this->realtime_func_, nullptr, 0);
        this->last_poll_tx_ = now;
        this->poll_pending_until_ = now + 400;   // hold Modbus until the 407-byte reply has landed (~40 ms @115200)
      }
      break;
  }
}

// ---------------------------------------------------------------- Modbus TCP

void SolaxAA55::accept_clients_() {
  struct sockaddr_storage ss; socklen_t sl = sizeof(ss);
  auto c = this->listen_->accept(reinterpret_cast<struct sockaddr *>(&ss), &sl);
  if (!c) return;
  c->setblocking(false);
  if (this->clients_.size() >= this->max_clients_) {
    // evict the most idle client
    size_t idx = 0;
    for (size_t i = 1; i < this->clients_.size(); i++)
      if (this->clients_[i].last_activity < this->clients_[idx].last_activity) idx = i;
    ESP_LOGW(TAG, "client limit reached, evicting idle client %u", (unsigned) idx);
    this->clients_.erase(this->clients_.begin() + idx);
    // fix queued/inflight client indices
    for (auto &r : this->queue_) { if (r.client == idx) r.client = 0xFF; else if (r.client != 0xFF && r.client > idx) r.client--; }
    if (this->inflight_) { if (this->inflight_req_.client == idx) this->inflight_req_.client = 0xFF; else if (this->inflight_req_.client != 0xFF && this->inflight_req_.client > idx) this->inflight_req_.client--; }
  }
  Client cl; cl.sock = std::move(c); cl.last_activity = millis();
  this->clients_.push_back(std::move(cl));
  ESP_LOGD(TAG, "modbus client connected (%u total)", (unsigned) this->clients_.size());
}

void SolaxAA55::deliver_(uint8_t client, const uint8_t *data, size_t len) {
  if (client >= this->clients_.size()) return;  // client went away
  auto &cl = this->clients_[client];
  ssize_t n = cl.sock->write(data, len);
  if (n < 0 && errno != EWOULDBLOCK && errno != EAGAIN) ESP_LOGW(TAG, "client write failed (%d)", errno);
  cl.last_activity = millis();
}

void SolaxAA55::send_modbus_exception_(uint8_t client, const std::vector<uint8_t> &tcp, uint8_t code) {
  if (tcp.size() < 8) return;
  uint8_t resp[9] = {tcp[0], tcp[1], 0, 0, 0, 3, tcp[6], (uint8_t) (tcp[7] | 0x80), code};
  this->deliver_(client, resp, sizeof(resp));
}

void SolaxAA55::service_clients_() {
  uint32_t now = millis();
  for (size_t i = 0; i < this->clients_.size();) {
    auto &cl = this->clients_[i];
    uint8_t buf[300];
    ssize_t n = cl.sock->read(buf, sizeof(buf));
    bool drop = false;
    if (n == 0) drop = true;
    else if (n < 0) { if (errno != EWOULDBLOCK && errno != EAGAIN) drop = true; }
    else {
      cl.last_activity = now;
      cl.rx.insert(cl.rx.end(), buf, buf + n);
      while (cl.rx.size() >= 6) {
        uint16_t mlen = (cl.rx[4] << 8) | cl.rx[5];
        if (mlen < 2 || mlen > 254 || cl.rx[2] != 0 || cl.rx[3] != 0) { ESP_LOGW(TAG, "bad MBAP from client %u, resync", (unsigned) i); cl.rx.clear(); break; }
        if (cl.rx.size() < 6u + mlen) break;
        if (this->queue_.size() >= MAX_QUEUE) {
          std::vector<uint8_t> tcp(cl.rx.begin(), cl.rx.begin() + 6 + mlen);
          this->send_modbus_exception_(i, tcp, 0x06);  // server busy
        } else {
          Request r; r.client = i; r.tcp.assign(cl.rx.begin(), cl.rx.begin() + 6 + mlen); r.queued_at = now;
          this->queue_.push_back(std::move(r));
        }
        cl.rx.erase(cl.rx.begin(), cl.rx.begin() + 6 + mlen);
      }
    }
    if (!drop && now - cl.last_activity > CLIENT_IDLE_MS) { ESP_LOGD(TAG, "closing idle client %u", (unsigned) i); drop = true; }
    if (drop) {
      this->clients_.erase(this->clients_.begin() + i);
      for (auto &r : this->queue_) { if (r.client == i) r.client = 0xFF; else if (r.client != 0xFF && r.client > i) r.client--; }
      if (this->inflight_) { if (this->inflight_req_.client == i) this->inflight_req_.client = 0xFF; else if (this->inflight_req_.client != 0xFF && this->inflight_req_.client > i) this->inflight_req_.client--; }
    } else {
      i++;
    }
  }
}

void SolaxAA55::run_modbus_() {
  uint32_t now = millis();
  if (this->inflight_) {
    if ((int32_t) (now - this->inflight_deadline_) >= 0) {
      if (!this->inflight_req_.retried && this->state_ == LinkState::ONLINE && this->can_send_()) {
        // the inverter occasionally misses a frame; one silent retry hides that from the client
        ESP_LOGD(TAG, "modbus reply missed (unit %u fc %u), retrying once", this->inflight_req_.tcp[6], this->inflight_req_.tcp[7]);
        this->inflight_req_.retried = true;
        this->stat_retry_++;
        this->send_tunnelled_(this->inflight_req_.tcp);
        this->inflight_deadline_ = now + this->response_timeout_;
        return;
      }
      ESP_LOGW(TAG, "modbus reply timeout (unit %u fc %u)", this->inflight_req_.tcp[6], this->inflight_req_.tcp[7]);
      this->send_modbus_exception_(this->inflight_req_.client, this->inflight_req_.tcp, 0x0B);
      this->inflight_ = false;
      this->stat_timeout_++;
      if (++this->consecutive_timeouts_ >= 5) { this->consecutive_timeouts_ = 0; this->go_handshake_("5 modbus timeouts"); }
    }
    return;
  }
  if (this->queue_.empty()) return;
  // drop requests that waited too long (link down) with an exception; 5 s = the HA integration's own timeout
  while (!this->queue_.empty() && now - this->queue_.front().queued_at > 5000) {
    auto &r = this->queue_.front();
    if (r.client != 0xFF) this->send_modbus_exception_(r.client, r.tcp, 0x0B);
    this->queue_.pop_front();
  }
  if (this->queue_.empty() || this->state_ != LinkState::ONLINE || !this->can_send_()) return;
  if ((int32_t) (now - this->poll_pending_until_) < 0) return;   // realtime poll reply still expected
  Request r = std::move(this->queue_.front());
  this->queue_.pop_front();
  if (r.client == 0xFF) return;
  this->send_tunnelled_(r.tcp);
  this->inflight_req_ = std::move(r);
  this->inflight_ = true;
  this->inflight_deadline_ = now + this->response_timeout_;
}

void SolaxAA55::send_tunnelled_(const std::vector<uint8_t> &tcp) {
  // AA 55 (n+9) 14 <slot> <MBAP+unit+PDU> <CRC16 lo hi> <SUM lo hi>
  std::vector<uint8_t> f;
  f.reserve(tcp.size() + 9);
  f.push_back(0xAA); f.push_back(0x55); f.push_back((uint8_t) (tcp.size() + 9)); f.push_back(CTRL_MODBUS); f.push_back(MODBUS_SLOT);
  f.insert(f.end(), tcp.begin(), tcp.end());
  uint16_t crc = modbus_crc_(tcp.data() + 6, tcp.size() - 6);
  f.push_back(crc & 0xFF); f.push_back(crc >> 8);
  uint16_t s = sum16_(f.data(), f.size());
  f.push_back(s & 0xFF); f.push_back(s >> 8);
  this->send_raw_(f.data(), f.size());
}

void SolaxAA55::loop() {
  this->drain_uart_();
  this->parse_frames_();
  this->run_link_();
  this->accept_clients_();
  this->service_clients_();
  this->run_modbus_();
}

}  // namespace solax_aa55
}  // namespace esphome
