/*
 * Test fixtures. The driver here is deliberately fabricated: it does not ship
 * with the application and never will, which is the point — §21.24 says
 * adding a driver needs no interface work, and the only way to prove that is
 * to render a schema the interface has never seen.
 */
import type { Device, Driver, SchemaField, SerialPort } from "./types";

/** One field of every type in the closed vocabulary of §5.5, plus a dependent one. */
export const FABRICATED_SCHEMA: SchemaField[] = [
  { key: "label", type: "string", label: "Label", required: true, help: "Written into the log beside every command" },
  { key: "retries", type: "int", label: "Retries", required: false, default: 3, min: 0, max: 10 },
  { key: "verbose", type: "bool", label: "Verbose logging", required: false, default: false },
  {
    key: "mode",
    type: "enum",
    label: "Mode",
    required: true,
    default: "alpha",
    options: [
      { value: "alpha", label: "Alpha" },
      { value: "beta", label: "Beta" },
    ],
  },
  { key: "control_port", type: "port", label: "Control port", required: true, default: 5000 },
  { key: "api_token", type: "password", label: "API token", required: false, encrypted: true },
  { key: "device_node", type: "device_path", label: "Device", required: false },
  { key: "address", type: "host", label: "IP address", required: true },
  {
    key: "beta_window",
    type: "int",
    label: "Beta window",
    required: false,
    default: 5,
    depends_on: { field: "mode", equals: "beta" },
  },
];

export const FABRICATED_DRIVER: Driver = {
  key: "fabricated",
  category: "video_matrix",
  name: "Fabricated device (test only)",
  transports: [
    {
      type: "tcp",
      config_schema: [
        { key: "host", type: "host", label: "IP address", required: true, help: "Must match the switch reservation" },
        { key: "port", type: "port", label: "Port", required: true, min: 1, max: 65535 },
      ],
      defaults: { port: 51325 },
    },
    {
      type: "serial",
      config_schema: [
        { key: "device_path", type: "device_path", label: "Device", required: true },
        {
          key: "baud",
          type: "enum",
          label: "Baud",
          required: true,
          default: "9600",
          options: [
            { value: "9600", label: "9600" },
            { value: "19200", label: "19200" },
          ],
        },
      ],
      defaults: { baud: "9600" },
    },
  ],
  config_schema: [
    { key: "midi_channel", type: "int", label: "MIDI channel", required: false, default: 1, min: 1, max: 16 },
    { key: "password", type: "password", label: "Password", required: false, encrypted: true },
  ],
  capabilities: {
    input_count: 20,
    output_count: 6,
    supports_scene_recall: true,
    supports_metering: true,
    supports_gain: false,
    min_db: -60,
    max_db: 10,
  },
};

export const DEVICE: Device = {
  id: 1,
  category: "video_matrix",
  driver_key: "fabricated",
  name: "House matrix",
  enabled: true,
  config: {
    transport: { type: "tcp", host: "10.2.30.71", port: 51325 },
    driver: { midi_channel: 1, password: { set: true } },
  },
  created_at: "2026-09-01T09:00:00+12:00",
  updated_at: "2026-09-10T19:42:11+12:00",
  state_key: "hdmi",
  status: { status: "connected", detail: null, host: "10.2.30.71", port: 51325 },
};

export const PORTS: SerialPort[] = [
  {
    path: "/dev/serial/by-id/usb-FTDI_USB-RS232_Cable_FTB6SPL2-if00-port0",
    label: "FTDI USB-RS232 Cable · FTB6SPL2",
    vendor_id: "0403",
    product_id: "6001",
    serial: "FTB6SPL2",
    in_use: false,
    in_use_by: null,
    stable: true,
  },
  {
    path: "/dev/serial/by-id/usb-FTDI_FT232R-if00-port0",
    label: "FTDI FT232R · AB0C1DEF",
    vendor_id: "0403",
    product_id: "6001",
    serial: "AB0C1DEF",
    in_use: true,
    in_use_by: "DMX output",
    stable: true,
  },
  {
    path: "/dev/ttyUSB2",
    label: "Prolific USB-Serial · no serial",
    vendor_id: "067b",
    product_id: "2303",
    serial: null,
    in_use: false,
    in_use_by: null,
    stable: false,
  },
];

/*
 * PJLink (§7.4, §21.24), shaped exactly like `proskenion/core/drivers/pjlink.py`
 * and `proskenion/core/transport/tcp.py` declare it: one TCP transport with
 * the shared host/port schema, defaulting to PJLink's registered port 4352,
 * one optional, encrypted password field and the minimum warm-up (§7.4,
 * an int, 0–600 s, default 60). Used to prove the generic
 * Devices screen renders and tests a real driver's schema, not just the
 * fabricated one above.
 */
export const PJLINK_DRIVER: Driver = {
  key: "pjlink",
  category: "projector",
  name: "PJLink (Class 1)",
  transports: [
    {
      type: "tcp",
      config_schema: [
        { key: "host", type: "host", label: "IP address", required: true, help: "Static address; must match the switch reservation" },
        { key: "port", type: "port", label: "Port", required: true, min: 1, max: 65535 },
      ],
      defaults: { port: 4352 },
    },
  ],
  config_schema: [
    {
      key: "password",
      type: "password",
      label: "Password",
      required: false,
      encrypted: true,
      help: "Leave blank if the projector has no PJLink password set (§6.10).",
    },
    {
      key: "min_warmup_s",
      type: "int",
      label: "Minimum warm-up (seconds)",
      required: false,
      default: 60,
      min: 0,
      max: 600,
      help: "The projector may report 'on' before its lamp is fully warm. Power-off is refused until this long after power-on.",
    },
  ],
  capabilities: { inputs: [], supports_authentication: true },
};

export const PJLINK_DEVICE: Device = {
  id: 2,
  category: "projector",
  driver_key: "pjlink",
  name: "House projector",
  enabled: true,
  config: {
    transport: { type: "tcp", host: "10.2.30.249", port: 4352 },
    driver: {},
  },
  created_at: "2026-09-01T09:00:00+12:00",
  updated_at: "2026-09-10T19:42:11+12:00",
  state_key: "projector",
  status: { status: "connected", detail: null, host: "10.2.30.249", port: 4352 },
};

/** The same device, with a password already stored (§6.10: never returned in plain). */
export const PJLINK_DEVICE_WITH_PASSWORD: Device = {
  ...PJLINK_DEVICE,
  config: {
    transport: { type: "tcp", host: "10.2.30.249", port: 4352 },
    driver: { password: { set: true } },
  },
};
