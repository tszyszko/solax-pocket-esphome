import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import uart
from esphome.const import CONF_ID

DEPENDENCIES = ["uart"]
AUTO_LOAD = ["socket", "network"]

solax_ns = cg.esphome_ns.namespace("solax_aa55")
SolaxAA55 = solax_ns.class_("SolaxAA55", cg.Component, uart.UARTDevice)

CONF_DONGLE_SERIAL = "dongle_serial"
CONF_MODBUS_PORT = "modbus_port"
CONF_RESPONSE_TIMEOUT = "response_timeout"
CONF_FRAME_GAP = "frame_gap"
CONF_FRAME_GAP_9600 = "frame_gap_9600"
CONF_ALLOW_115200 = "allow_115200"
CONF_KEEPALIVE_INTERVAL = "keepalive_interval"
CONF_POLL_INTERVAL = "poll_interval"
CONF_SILENCE_TIMEOUT = "silence_timeout"
CONF_MAX_CLIENTS = "max_clients"


def _serial(value):
    value = cv.string_strict(value)
    if len(value) != 10:
        raise cv.Invalid("dongle_serial must be exactly 10 ASCII characters")
    return value


CONFIG_SCHEMA = cv.Schema({
    cv.GenerateID(): cv.declare_id(SolaxAA55),
    cv.Required(CONF_DONGLE_SERIAL): _serial,
    cv.Optional(CONF_MODBUS_PORT, default=502): cv.port,
    cv.Optional(CONF_RESPONSE_TIMEOUT, default="1000ms"): cv.positive_time_period_milliseconds,
    cv.Optional(CONF_FRAME_GAP, default="100ms"): cv.positive_time_period_milliseconds,
    cv.Optional(CONF_FRAME_GAP_9600, default="650ms"): cv.positive_time_period_milliseconds,
    cv.Optional(CONF_ALLOW_115200, default=True): cv.boolean,
    cv.Optional(CONF_KEEPALIVE_INTERVAL, default="10s"): cv.positive_time_period_milliseconds,
    cv.Optional(CONF_POLL_INTERVAL, default="5s"): cv.positive_time_period_milliseconds,
    cv.Optional(CONF_SILENCE_TIMEOUT, default="30s"): cv.positive_time_period_milliseconds,
    cv.Optional(CONF_MAX_CLIENTS, default=4): cv.int_range(min=1, max=8),
}).extend(cv.COMPONENT_SCHEMA).extend(uart.UART_DEVICE_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await uart.register_uart_device(var, config)
    cg.add(var.set_dongle_serial(config[CONF_DONGLE_SERIAL]))
    cg.add(var.set_modbus_port(config[CONF_MODBUS_PORT]))
    cg.add(var.set_response_timeout(config[CONF_RESPONSE_TIMEOUT]))
    cg.add(var.set_frame_gap(config[CONF_FRAME_GAP], config[CONF_FRAME_GAP_9600]))
    cg.add(var.set_allow_115200(config[CONF_ALLOW_115200]))
    cg.add(var.set_keepalive_interval(config[CONF_KEEPALIVE_INTERVAL]))
    cg.add(var.set_poll_interval(config[CONF_POLL_INTERVAL]))
    cg.add(var.set_silence_timeout(config[CONF_SILENCE_TIMEOUT]))
    cg.add(var.set_max_clients(config[CONF_MAX_CLIENTS]))
