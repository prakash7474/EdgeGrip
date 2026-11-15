/*
 * Phase 5b device firmware for the robotic hand — ESP32 / Arduino.
 *
 * >>> UNTESTED. This sketch has never been compiled for, flashed to, or run on
 * >>> real hardware. Do not use it on a machine until you have followed
 * >>> docs/hardware.md and verified it on a bench with the hand unloaded.
 *
 * Protocol: docs/device_protocol.md
 *   host -> device   command_id|command|parameters|checksum\n
 *   device -> host   ACK|command_id|command|detail\n
 *                    NACK|command_id|reason|detail\n
 *
 * Safety behaviour implemented here:
 *   - boots in a safe state with both servos detached; the first valid
 *     heartbeat attaches them without moving,
 *   - rejects frames with a bad checksum, bad format, unknown command,
 *     invalid parameter or out-of-range angle/speed,
 *   - remembers the last 32 command ids and re-ACKs duplicates without moving,
 *   - if no valid heartbeat arrives within HEARTBEAT_TIMEOUT_MS, detaches both
 *     servos and ignores motion commands until the next heartbeat.
 *
 * Libraries:
 *   ESP32: ESP32Servo (Library Manager: "ESP32Servo" by Kevin Harrington)
 *   AVR  : Servo (bundled with the Arduino IDE)
 */

#if defined(ARDUINO_ARCH_ESP32)
  #include <ESP32Servo.h>
#else
  #include <Servo.h>
#endif

// ---------------------------------------------------------------------------
// Pins: CHANGE THESE FOR YOUR BOARD AND WIRING.
// On AVR these are PWM-capable pins on an Uno/Nano.
// ---------------------------------------------------------------------------
#if defined(ARDUINO_ARCH_ESP32)
const uint8_t WRIST_PIN = 18;
const uint8_t GRIPPER_PIN = 19;
#else
const uint8_t WRIST_PIN = 9;
const uint8_t GRIPPER_PIN = 10;
#endif

// ---------------------------------------------------------------------------
// Limits. These must match actuator/protocol.py.
// ---------------------------------------------------------------------------
const int MIN_ANGLE = 0;
const int MAX_ANGLE = 180;
const int MIN_SPEED = 1;
const int MAX_SPEED = 100;
const int DEFAULT_SPEED = 30;

// Gripper end positions in servo degrees, not calibrated. Measure yours.
const uint8_t GRIP_ANGLE = 120;
const uint8_t RELEASE_ANGLE = 35;

// ---------------------------------------------------------------------------
// Timing and protocol sizes.
// ---------------------------------------------------------------------------
// CONFIGURABLE: no heartbeat for this long -> safe state.
const unsigned long HEARTBEAT_TIMEOUT_MS = 2000UL;

// Roughly 3.3 ms per degree at speed 100; slower speeds scale linearly.
const unsigned long STEP_BASE_MS = 330UL;

const uint8_t MAX_FRAME_BYTES = 192;   // including the trailing newline
const uint8_t DEDUP_SIZE = 32;
const uint8_t COMMAND_ID_MAX = 36;
const uint8_t DETAIL_MAX = 40;

Servo wristServo;
Servo gripperServo;

char lineBuffer[MAX_FRAME_BYTES];
uint8_t lineLength = 0;

char seenIds[DEDUP_SIZE][COMMAND_ID_MAX + 1];
uint8_t seenCount = 0;
uint8_t seenNext = 0;

bool safeState = true;
unsigned long lastHeartbeatMs = 0;

int wristCurrent = 90;
int wristTarget = 90;
int wristSpeed = DEFAULT_SPEED;
bool wristMoving = false;
unsigned long wristLastStepMs = 0;

int gripperCurrent = RELEASE_ANGLE;
int gripperTarget = RELEASE_ANGLE;
int gripperSpeed = DEFAULT_SPEED;
bool gripperMoving = false;
unsigned long gripperLastStepMs = 0;

// ---------------------------------------------------------------------------
// Checksum: 32-bit FNV-1a over "command_id|command|parameters".
// ---------------------------------------------------------------------------
uint32_t fnv1aUpdate(uint32_t hash, const char *text) {
  while (*text != '\0') {
    hash ^= (uint8_t)(*text++);
    hash *= 16777619UL;
  }
  return hash;
}

uint32_t frameChecksum(const char *commandId, const char *command,
                       const char *parameters) {
  uint32_t hash = 2166136261UL;
  hash = fnv1aUpdate(hash, commandId);
  hash ^= (uint8_t)'|';
  hash *= 16777619UL;
  hash = fnv1aUpdate(hash, command);
  hash ^= (uint8_t)'|';
  hash *= 16777619UL;
  return fnv1aUpdate(hash, parameters);
}

bool parseHex32(const char *text, uint32_t &value) {
  if (strlen(text) != 8) {
    return false;
  }
  uint32_t result = 0;
  for (uint8_t i = 0; i < 8; ++i) {
    char c = text[i];
    uint8_t digit;
    if (c >= '0' && c <= '9') {
      digit = (uint8_t)(c - '0');
    } else if (c >= 'A' && c <= 'F') {
      digit = (uint8_t)(c - 'A' + 10);
    } else if (c >= 'a' && c <= 'f') {
      digit = (uint8_t)(c - 'a' + 10);
    } else {
      return false;
    }
    result = (result << 4) | digit;
  }
  value = result;
  return true;
}

// ---------------------------------------------------------------------------
// Replies
// ---------------------------------------------------------------------------
void printDetail(const char *detail) {
  if (detail == nullptr || detail[0] == '\0') {
    Serial.print('-');
    return;
  }
  uint8_t printed = 0;
  while (*detail != '\0' && printed < DETAIL_MAX) {
    char c = *detail++;
    if (c != '|' && c != '\r' && c != '\n') {
      Serial.print(c);
    }
    ++printed;
  }
}

void sendAck(const char *commandId, const char *command, const char *detail) {
  Serial.print(F("ACK|"));
  Serial.print(commandId);
  Serial.print('|');
  Serial.print(command);
  Serial.print('|');
  printDetail(detail);
  Serial.print('\n');
}

void sendNack(const char *commandId, const char *reason, const char *detail) {
  Serial.print(F("NACK|"));
  Serial.print(commandId);
  Serial.print('|');
  Serial.print(reason);
  Serial.print('|');
  printDetail(detail);
  Serial.print('\n');
}

// ---------------------------------------------------------------------------
// Frame parsing (manual split, because strtok would drop empty fields)
// ---------------------------------------------------------------------------
uint8_t splitFields(char *line, char *fields[], uint8_t maxFields) {
  uint8_t count = 0;
  char *start = line;
  for (char *cursor = line;; ++cursor) {
    if (*cursor == '|' || *cursor == '\0') {
      if (count >= maxFields) {
        return (uint8_t)(maxFields + 1);
      }
      fields[count++] = start;
      if (*cursor == '\0') {
        break;
      }
      *cursor = '\0';
      start = cursor + 1;
    }
  }
  return count;
}

bool validCommandId(const char *value) {
  size_t length = strlen(value);
  if (length < 1 || length > COMMAND_ID_MAX) {
    return false;
  }
  for (size_t i = 0; i < length; ++i) {
    char c = value[i];
    if (!(isalnum((unsigned char)c) || c == '-' || c == '_')) {
      return false;
    }
  }
  return true;
}

bool parseIntStrict(const char *text, int &value) {
  if (*text == '\0') {
    return false;
  }
  char *end = nullptr;
  long parsed = strtol(text, &end, 10);
  if (end == text || *end != '\0') {
    return false;
  }
  if (parsed < -9999L || parsed > 9999L) {
    return false;
  }
  value = (int)parsed;
  return true;
}

enum ParamResult { PARAMS_OK, PARAMS_INVALID };

ParamResult parseParams(char *text, int &angle, bool &hasAngle,
                        int &speed, bool &hasSpeed) {
  hasAngle = false;
  hasSpeed = false;
  if (text[0] == '\0') {
    return PARAMS_OK;
  }
  char *token = strtok(text, ",");
  while (token != nullptr) {
    char *equals = strchr(token, '=');
    if (equals == nullptr) {
      return PARAMS_INVALID;
    }
    *equals = '\0';
    int value = 0;
    if (!parseIntStrict(equals + 1, value)) {
      return PARAMS_INVALID;
    }
    if (strcmp(token, "angle") == 0) {
      if (hasAngle) {
        return PARAMS_INVALID;
      }
      angle = value;
      hasAngle = true;
    } else if (strcmp(token, "speed") == 0) {
      if (hasSpeed) {
        return PARAMS_INVALID;
      }
      speed = value;
      hasSpeed = true;
    } else {
      return PARAMS_INVALID;
    }
    token = strtok(nullptr, ",");
  }
  return PARAMS_OK;
}

bool isKnownCommand(const char *command) {
  return strcmp(command, "grip") == 0 || strcmp(command, "release") == 0 ||
         strcmp(command, "move_to") == 0 || strcmp(command, "stop") == 0 ||
         strcmp(command, "heartbeat") == 0;
}

bool isMotionCommand(const char *command) {
  return strcmp(command, "grip") == 0 || strcmp(command, "release") == 0 ||
         strcmp(command, "move_to") == 0;
}

// ---------------------------------------------------------------------------
// Dedup ring: the last 32 accepted command ids, oldest evicted
// ---------------------------------------------------------------------------
bool alreadySeen(const char *commandId) {
  for (uint8_t i = 0; i < seenCount; ++i) {
    if (strcmp(seenIds[i], commandId) == 0) {
      return true;
    }
  }
  return false;
}

void rememberCommand(const char *commandId) {
  strncpy(seenIds[seenNext], commandId, COMMAND_ID_MAX);
  seenIds[seenNext][COMMAND_ID_MAX] = '\0';
  seenNext = (uint8_t)((seenNext + 1) % DEDUP_SIZE);
  if (seenCount < DEDUP_SIZE) {
    ++seenCount;
  }
}

// ---------------------------------------------------------------------------
// Safe state and heartbeat
// ---------------------------------------------------------------------------
void enterSafeState() {
  wristMoving = false;
  gripperMoving = false;
  // detach() removes the PWM pulse: no torque, no holding force.
  wristServo.detach();
  gripperServo.detach();
  safeState = true;
}

void onHeartbeat() {
  lastHeartbeatMs = millis();
  if (safeState) {
    // Attach, then command the current position so the servos do not jump to
    // an old target. There can still be a small movement on attach.
    wristServo.attach(WRIST_PIN);
    gripperServo.attach(GRIPPER_PIN);
    wristServo.write(wristCurrent);
    gripperServo.write(gripperCurrent);
    safeState = false;
  }
}

// ---------------------------------------------------------------------------
// Motion: one degree per step, serviced from loop() so heartbeats keep arriving
// ---------------------------------------------------------------------------
unsigned long stepIntervalMs(int speedPercent) {
  if (speedPercent < MIN_SPEED) {
    speedPercent = MIN_SPEED;
  }
  if (speedPercent > MAX_SPEED) {
    speedPercent = MAX_SPEED;
  }
  unsigned long interval = STEP_BASE_MS / (unsigned long)speedPercent;
  return interval == 0 ? 1 : interval;
}

void startWristMove(int target, int speedPercent) {
  wristTarget = target;
  wristSpeed = speedPercent;
  wristMoving = wristTarget != wristCurrent;
  wristLastStepMs = millis();
}

void startGripperMove(int target, int speedPercent) {
  gripperTarget = target;
  gripperSpeed = speedPercent;
  gripperMoving = gripperTarget != gripperCurrent;
  gripperLastStepMs = millis();
}

void serviceWrist() {
  if (!wristMoving) {
    return;
  }
  unsigned long now = millis();
  if (now - wristLastStepMs < stepIntervalMs(wristSpeed)) {
    return;
  }
  wristLastStepMs = now;
  if (wristCurrent < wristTarget) {
    ++wristCurrent;
  } else if (wristCurrent > wristTarget) {
    --wristCurrent;
  }
  wristServo.write(wristCurrent);
  if (wristCurrent == wristTarget) {
    wristMoving = false;
  }
}

void serviceGripper() {
  if (!gripperMoving) {
    return;
  }
  unsigned long now = millis();
  if (now - gripperLastStepMs < stepIntervalMs(gripperSpeed)) {
    return;
  }
  gripperLastStepMs = now;
  if (gripperCurrent < gripperTarget) {
    ++gripperCurrent;
  } else if (gripperCurrent > gripperTarget) {
    --gripperCurrent;
  }
  gripperServo.write(gripperCurrent);
  if (gripperCurrent == gripperTarget) {
    gripperMoving = false;
  }
}

void serviceWatchdog() {
  if (!safeState && (millis() - lastHeartbeatMs) > HEARTBEAT_TIMEOUT_MS) {
    enterSafeState();
  }
}

// ---------------------------------------------------------------------------
// Command handling
// ---------------------------------------------------------------------------
void executeCommand(const char *command, int angle, int speed, bool hasSpeed) {
  int effectiveSpeed = hasSpeed ? speed : DEFAULT_SPEED;
  if (strcmp(command, "grip") == 0) {
    startGripperMove(GRIP_ANGLE, effectiveSpeed);
  } else if (strcmp(command, "release") == 0) {
    startGripperMove(RELEASE_ANGLE, effectiveSpeed);
  } else if (strcmp(command, "move_to") == 0) {
    startWristMove(angle, effectiveSpeed);
  } else if (strcmp(command, "stop") == 0) {
    wristMoving = false;
    gripperMoving = false;
  }
}

void handleLine(char *line) {
  // Echo a usable command id in NACKs even when the rest is malformed.
  char hint[COMMAND_ID_MAX + 1];
  size_t hintLength = 0;
  while (hintLength < COMMAND_ID_MAX && line[hintLength] != '\0' &&
         line[hintLength] != '|') {
    hint[hintLength] = line[hintLength];
    ++hintLength;
  }
  hint[hintLength] = '\0';
  // A first field longer than COMMAND_ID_MAX was truncated; do not echo it.
  if (line[hintLength] != '\0' && line[hintLength] != '|') {
    strcpy(hint, "-");
  } else if (!validCommandId(hint)) {
    strcpy(hint, "-");
  }

  char *fields[4];
  uint8_t fieldCount = splitFields(line, fields, 4);
  if (fieldCount != 4) {
    sendNack(hint, "format", "expected 4 fields");
    return;
  }
  const char *commandId = fields[0];
  const char *command = fields[1];
  const char *parametersText = fields[2];
  const char *checksumText = fields[3];

  if (!validCommandId(commandId)) {
    sendNack("-", "format", "bad command_id");
    return;
  }
  uint32_t expected = frameChecksum(commandId, command, parametersText);
  uint32_t received = 0;
  if (!parseHex32(checksumText, received)) {
    sendNack(commandId, "format", "bad checksum field");
    return;
  }
  if (expected != received) {
    sendNack(commandId, "checksum", "checksum mismatch");
    return;
  }
  if (!isKnownCommand(command)) {
    sendNack(commandId, "unknown_command", command);
    return;
  }

  int angle = 0;
  int speed = 0;
  bool hasAngle = false;
  bool hasSpeed = false;
  if (parseParams(fields[2], angle, hasAngle, speed, hasSpeed) != PARAMS_OK) {
    sendNack(commandId, "invalid_parameter", "bad parameter list");
    return;
  }
  if (strcmp(command, "move_to") == 0) {
    if (!hasAngle) {
      sendNack(commandId, "invalid_parameter", "angle required");
      return;
    }
  } else if (hasAngle) {
    // Only move_to takes an angle.
    sendNack(commandId, "invalid_parameter", "angle not allowed");
    return;
  }
  if (isMotionCommand(command) == false && hasSpeed) {
    sendNack(commandId, "invalid_parameter", "speed not allowed");
    return;
  }
  if (hasAngle && (angle < MIN_ANGLE || angle > MAX_ANGLE)) {
    sendNack(commandId, "limit", "angle out of range");
    return;
  }
  if (hasSpeed && (speed < MIN_SPEED || speed > MAX_SPEED)) {
    sendNack(commandId, "limit", "speed out of range");
    return;
  }

  // Heartbeat is handled before dedup so a repeated heartbeat still resets the
  // watchdog. It is also remembered, so it consumes a dedup slot.
  if (strcmp(command, "heartbeat") == 0) {
    bool duplicate = alreadySeen(commandId);
    rememberCommand(commandId);
    onHeartbeat();
    sendAck(commandId, command, duplicate ? "duplicate" : "");
    return;
  }
  if (alreadySeen(commandId)) {
    sendAck(commandId, command, "duplicate");
    return;
  }
  if (safeState && isMotionCommand(command)) {
    sendNack(commandId, "safe_state", "heartbeat required");
    return;
  }

  executeCommand(command, angle, speed, hasSpeed);
  rememberCommand(commandId);
  // ACK means "accepted and applied", not "motion finished".
  sendAck(commandId, command, "");
}

void serviceSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') {
      continue;
    }
    if (c == '\n') {
      lineBuffer[lineLength] = '\0';
      handleLine(lineBuffer);
      lineLength = 0;
      continue;
    }
    if (lineLength < MAX_FRAME_BYTES - 1) {
      lineBuffer[lineLength++] = c;
    } else {
      lineLength = 0;
      sendNack("-", "format", "frame too long");
    }
  }
}

// ---------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  wristServo.detach();
  gripperServo.detach();
  lastHeartbeatMs = millis();
  safeState = true;
}

void loop() {
  serviceSerial();
  serviceWrist();
  serviceGripper();
  serviceWatchdog();
}
