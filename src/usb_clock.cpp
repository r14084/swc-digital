// USB Clock — minimal custom firmware for the ESP8266 Smart Weather Clock.
//
// The onboard CH340 exposes Serial at 115200 baud. Configuration is line based
// so shell scripts and small host tools can control the clock without WiFi.
// Type HELP over serial to list commands.

#include <Arduino.h>
#include <errno.h>
#include <EEPROM.h>
#include <LittleFS.h>
#include <SPI.h>
#include <Arduino_GFX_Library.h>

#include "board_esp8266.h"
#include "thai_greeting_bitmap.h"

namespace {

constexpr uint16_t SCREEN_W = 240;
constexpr uint16_t SCREEN_H = 240;
constexpr uint32_t CONFIG_MAGIC = 0x5542434B;  // "UBCK"
constexpr uint32_t GALLERY_MAGIC = 0x47414C52;  // "GALR"
constexpr uint32_t REMINDER_MAGIC = 0x524D4E44;  // "RMND"
constexpr uint16_t CONFIG_VERSION = 1;
constexpr size_t EEPROM_BYTES = 512;
constexpr size_t REMINDER_OFFSET = 160;

constexpr uint16_t BLACK   = 0x0000;
constexpr uint16_t WHITE   = 0xFFFF;
constexpr uint16_t GRAY    = 0x8410;
constexpr uint16_t DARK    = 0x18E3;
constexpr uint16_t GREEN   = 0x07E0;
constexpr uint16_t RED     = 0xF800;
constexpr uint16_t BLUE    = 0x001F;
constexpr uint16_t AMBER   = 0xFD20;
constexpr uint16_t CLAUDE_COLOR = 0xDBAA;  // rgb(218, 119, 86)

enum class ScreenMode : uint8_t { USAGE, BTC, CLOCK, THAI, GALLERY, FACE };
enum class FaceMood : uint8_t {
  AUTO,
  NEUTRAL,
  HAPPY,
  FOCUS,
  CURIOUS,
  SLEEPY,
  ALERT,
  CELEBRATE,
};
constexpr uint8_t DISPLAY_SCREEN_MASK = 0x03;
constexpr uint8_t DISPLAY_SCREEN_BTC = 0x01;
constexpr uint8_t DISPLAY_SCREEN_FACE = 0x02;
constexpr uint8_t DISPLAY_MOOD_SHIFT = 2;
constexpr uint8_t DISPLAY_MOOD_MASK = 0x1C;
constexpr uint8_t GALLERY_SLOTS = 7;
constexpr size_t GALLERY_BYTES = SCREEN_W * SCREEN_H * 2;
constexpr uint8_t BTC_CANDLES = 24;
constexpr size_t BTC_HEX_LENGTH = BTC_CANDLES * 4 * 2;
constexpr uint8_t REMINDER_SLOTS = 8;
constexpr uint16_t EMPTY_REMINDER = 0xFFFF;
constexpr uint32_t ALERT_DURATION_MILLIS = 60000UL;

struct UsageSnapshot {
  uint8_t claude[3];
  uint8_t codex[3];
  bool valid;
};

struct CryptoSnapshot {
  uint32_t priceCents;
  int16_t changeBps;
  uint8_t candles[BTC_CANDLES][4];  // normalized open, high, low, close
  bool valid;
};

struct PersistedConfig {
  uint32_t magic;
  uint16_t version;
  char title[25];
  char city[25];
  uint8_t brightness;
  uint8_t rotation;
  uint8_t backlightInverted;
  // Packed persisted screen selection and Face mood. Values 0/1 retain the
  // original none/BTC meaning used by earlier custom firmware builds.
  uint8_t displayState;
  uint16_t accent;
  uint32_t checksum;
};

struct GalleryPlayback {
  uint32_t magic;
  uint8_t slot;
  uint8_t playing;
  uint16_t seconds;
  uint32_t checksum;
};

struct ReminderSlot {
  uint16_t minuteOfDay;
  char label[21];
  uint8_t enabled;
};

struct ReminderStore {
  uint32_t magic;
  ReminderSlot slots[REMINDER_SLOTS];
  uint32_t checksum;
};

static_assert(sizeof(ReminderStore) <= EEPROM_BYTES - REMINDER_OFFSET,
              "Reminder store exceeds reserved EEPROM region");

PersistedConfig cfg;
Arduino_DataBus* bus = nullptr;
Arduino_GFX* display = nullptr;

char serialLine[256];
size_t serialLength = 0;
bool screenDirty = true;
bool usageDirty = false;
bool cryptoDirty = false;
bool testPatternActive = false;
ScreenMode screenMode = ScreenMode::USAGE;
uint8_t gallerySlot = 0;
File galleryUpload;
size_t galleryRemaining = 0;
uint32_t galleryHash = 2166136261UL;
uint8_t galleryUploadSlot = 0;
bool galleryUploadComplete = false;
bool galleryPlaying = false;
uint32_t galleryIntervalMillis = 10000UL;
uint32_t lastGalleryAdvance = 0;
bool timeWasSet = false;
uint16_t baseMinuteOfDay = 0;
uint32_t baseTimeMillis = 0;
UsageSnapshot usage = {};
CryptoSnapshot crypto = {};
uint8_t activeAccount = 0xFF;  // 0-2 = C1-C3, 3-5 = X1-X3
uint8_t usageHistory[6][12] = {};
uint8_t usageHistoryCount = 0;
uint32_t lastUsageMillis = 0;
uint32_t lastSyncMinuteDrawn = UINT32_MAX;
uint32_t resetDurationMillis[6] = {};
uint32_t resetBaseMillis = 0;
uint8_t resetKnownMask = 0;  // bit 0-2 = C1-C3, bit 3-5 = X1-X3
ReminderStore reminders = {};
uint8_t reminderFiredMask = 0;
uint16_t lastReminderCheckMinute = EMPTY_REMINDER;
ScreenMode alertReturnMode = ScreenMode::USAGE;
bool alertActive = false;
uint32_t alertStartMillis = 0;
uint8_t alertReminderSlot = 0;
bool serialDiscardingLine = false;
FaceMood faceMood = FaceMood::AUTO;
FaceMood autoFaceMood = FaceMood::NEUTRAL;
bool autoFaceMoodSet = false;
FaceMood lastRenderedFaceMood = FaceMood::AUTO;
bool faceBlinking = false;
uint8_t faceBlinkPhase = 0;
int8_t faceLookOffset = 0;
uint32_t faceBlinkStartMillis = 0;
uint32_t nextFaceBlinkMillis = 0;
uint32_t nextFaceLookMillis = 0;
uint32_t lastFaceEffectFrame = UINT32_MAX;
bool faceDirty = false;

const char* GALLERY_UPLOAD_PATH = "/gallery-upload.tmp";

class ClockST7789 : public Arduino_ST7789 {
 public:
  using Arduino_ST7789::Arduino_ST7789;

  bool begin(int32_t speed = GFX_NOT_DEFINED) override {
    _override_datamode = SPI_MODE3;
    return Arduino_TFT::begin(speed);
  }
};

uint32_t checksumConfig(const PersistedConfig& value) {
  const uint8_t* bytes = reinterpret_cast<const uint8_t*>(&value);
  const size_t length = offsetof(PersistedConfig, checksum);
  uint32_t hash = 2166136261UL;
  for (size_t i = 0; i < length; ++i) {
    hash ^= bytes[i];
    hash *= 16777619UL;
  }
  return hash;
}

uint32_t checksumGallery(const GalleryPlayback& value) {
  const uint8_t* bytes = reinterpret_cast<const uint8_t*>(&value);
  const size_t length = offsetof(GalleryPlayback, checksum);
  uint32_t hash = 2166136261UL;
  for (size_t i = 0; i < length; ++i) {
    hash ^= bytes[i];
    hash *= 16777619UL;
  }
  return hash;
}

uint32_t checksumReminders(const ReminderStore& value) {
  const uint8_t* bytes = reinterpret_cast<const uint8_t*>(&value);
  const size_t length = offsetof(ReminderStore, checksum);
  uint32_t hash = 2166136261UL;
  for (size_t i = 0; i < length; ++i) {
    hash ^= bytes[i];
    hash *= 16777619UL;
  }
  return hash;
}

void setReminderDefaults() {
  memset(&reminders, 0, sizeof(reminders));
  reminders.magic = REMINDER_MAGIC;
  for (uint8_t i = 0; i < REMINDER_SLOTS; ++i) {
    reminders.slots[i].minuteOfDay = EMPTY_REMINDER;
  }
  reminders.checksum = checksumReminders(reminders);
}

void loadReminders() {
  EEPROM.get(REMINDER_OFFSET, reminders);
  bool valid = reminders.magic == REMINDER_MAGIC &&
               reminders.checksum == checksumReminders(reminders);
  for (uint8_t i = 0; valid && i < REMINDER_SLOTS; ++i) {
    const ReminderSlot& slot = reminders.slots[i];
    valid = slot.minuteOfDay < 1440 || slot.minuteOfDay == EMPTY_REMINDER;
    valid = valid && slot.enabled <= 1 && slot.label[20] == '\0';
  }
  if (!valid) setReminderDefaults();
}

bool saveReminders() {
  reminders.checksum = checksumReminders(reminders);
  EEPROM.put(REMINDER_OFFSET, reminders);
  return EEPROM.commit();
}

void saveGalleryPlayback() {
  GalleryPlayback value = {GALLERY_MAGIC, gallerySlot,
                           static_cast<uint8_t>(galleryPlaying),
                           static_cast<uint16_t>(galleryIntervalMillis / 1000UL), 0};
  value.checksum = checksumGallery(value);
  EEPROM.put(128, value);
  EEPROM.commit();
}

void loadGalleryPlayback() {
  GalleryPlayback value = {};
  EEPROM.get(128, value);
  if (value.magic == GALLERY_MAGIC && value.checksum == checksumGallery(value) &&
      value.slot < GALLERY_SLOTS && value.playing <= 1 &&
      value.seconds >= 3 && value.seconds <= 300) {
    gallerySlot = value.slot;
    galleryPlaying = value.playing;
    galleryIntervalMillis = static_cast<uint32_t>(value.seconds) * 1000UL;
    if (galleryPlaying) screenMode = ScreenMode::GALLERY;
  }
}

void copyText(char* destination, size_t size, const char* value) {
  if (!value) value = "";
  strlcpy(destination, value, size);
}

void setDefaults() {
  memset(&cfg, 0, sizeof(cfg));
  cfg.magic = CONFIG_MAGIC;
  cfg.version = CONFIG_VERSION;
  copyText(cfg.title, sizeof(cfg.title), "CUSTOM CLOCK");
  copyText(cfg.city, sizeof(cfg.city), "BANGKOK");
  cfg.brightness = 72;
  cfg.rotation = 0;
  cfg.backlightInverted = 1;
  cfg.accent = 0x07FF;  // cyan
  cfg.checksum = checksumConfig(cfg);
}

bool loadConfig() {
  EEPROM.begin(EEPROM_BYTES);
  EEPROM.get(0, cfg);
  const bool valid = cfg.magic == CONFIG_MAGIC &&
                     cfg.version == CONFIG_VERSION &&
                     cfg.checksum == checksumConfig(cfg) &&
                     cfg.brightness <= 100 && cfg.rotation <= 3 &&
                     cfg.backlightInverted <= 1 &&
                     (cfg.displayState & ~0x1F) == 0 &&
                     (cfg.displayState & DISPLAY_SCREEN_MASK) <= DISPLAY_SCREEN_FACE &&
                     ((cfg.displayState & DISPLAY_MOOD_MASK) >> DISPLAY_MOOD_SHIFT) <=
                         static_cast<uint8_t>(FaceMood::CELEBRATE) &&
                     memchr(cfg.title, '\0', sizeof(cfg.title)) != nullptr &&
                     memchr(cfg.city, '\0', sizeof(cfg.city)) != nullptr;
  if (!valid) setDefaults();
  return valid;
}

bool saveConfig() {
  cfg.checksum = checksumConfig(cfg);
  EEPROM.put(0, cfg);
  return EEPROM.commit();
}

FaceMood persistedFaceMood() {
  return static_cast<FaceMood>((cfg.displayState & DISPLAY_MOOD_MASK) >>
                               DISPLAY_MOOD_SHIFT);
}

void setDisplayPersistence(ScreenMode mode) {
  uint8_t screen = mode == ScreenMode::BTC ? DISPLAY_SCREEN_BTC :
                   mode == ScreenMode::FACE ? DISPLAY_SCREEN_FACE : 0;
  uint8_t value = (cfg.displayState & DISPLAY_MOOD_MASK) | screen;
  if (cfg.displayState == value) return;
  cfg.displayState = value;
  saveConfig();
}

void persistFaceMood(FaceMood mood) {
  uint8_t value = DISPLAY_SCREEN_FACE |
                  (static_cast<uint8_t>(mood) << DISPLAY_MOOD_SHIFT);
  faceMood = mood;
  if (cfg.displayState == value) return;
  cfg.displayState = value;
  saveConfig();
}

void applyBrightness() {
  uint8_t brightness = min<uint8_t>(cfg.brightness, 100);
  int duty = static_cast<int>(brightness) * 255 / 100;
  if (cfg.backlightInverted) duty = 255 - duty;
  analogWrite(TFT_BL, duty);
}

int textWidth(const char* text, uint8_t size) {
  return static_cast<int>(strlen(text)) * 6 * size;
}

void centered(const char* text, int y, uint8_t size, uint16_t color) {
  int x = (SCREEN_W - textWidth(text, size)) / 2;
  if (x < 0) x = 0;
  display->setTextSize(size);
  display->setTextColor(color);
  display->setCursor(x, y);
  display->print(text);
}

void clippedLabel(const char* text, int x, int y, uint8_t size, uint16_t color,
                  size_t maxChars) {
  char clipped[30];
  strlcpy(clipped, text, min(sizeof(clipped), maxChars + 1));
  display->setTextSize(size);
  display->setTextColor(color);
  display->setCursor(x, y);
  display->print(clipped);
}

uint16_t usageColor(uint8_t percent, uint16_t normalColor) {
  if (percent >= 90) return RED;
  if (percent >= 70) return AMBER;
  return normalColor;
}

void drawUsageBar(const char* label, uint8_t percent, uint8_t previousPercent,
                  int y, uint16_t normalColor,
                  bool active) {
  constexpr int BAR_X = 52;
  constexpr int BAR_W = 136;
  constexpr int BAR_H = 8;
  char percentText[6];
  snprintf(percentText, sizeof(percentText), "%u%%", percent);

  display->setTextSize(1);
  display->setTextColor(active ? normalColor : WHITE);
  display->setCursor(14, y - 1);
  display->print(label);
  display->setTextColor(percent > previousPercent ? AMBER : GRAY);
  display->setCursor(31, y - 1);
  display->print(percent > previousPercent ? '^' :
                 percent < previousPercent ? 'v' : '=');
  display->setTextColor(usageColor(percent, normalColor));
  display->setCursor(198, y - 1);
  display->print(percentText);
  if (active) display->fillCircle(44, y + 3, 2, normalColor);

  display->fillRoundRect(BAR_X, y, BAR_W, BAR_H, 3, DARK);
  int filled = static_cast<int>(percent) * BAR_W / 100;
  uint16_t color = usageColor(percent, normalColor);
  if (filled) display->fillRoundRect(BAR_X, y, filled, BAR_H, 3, color);
}

uint8_t lowestAccount(const uint8_t values[3]) {
  uint8_t lowest = 0;
  for (uint8_t i = 1; i < 3; ++i) {
    if (values[i] < values[lowest]) lowest = i;
  }
  return lowest;
}

uint8_t highestAccount(const uint8_t values[3]) {
  uint8_t highest = 0;
  for (uint8_t i = 1; i < 3; ++i) {
    if (values[i] > values[highest]) highest = i;
  }
  return highest;
}

int32_t resetMinutesLeft(uint8_t index) {
  if (!(resetKnownMask & (1 << index))) return -1;
  uint32_t elapsed = millis() - resetBaseMillis;
  if (elapsed >= resetDurationMillis[index]) return -1;
  uint32_t remaining = resetDurationMillis[index] - elapsed;
  return static_cast<int32_t>((remaining + 59999UL) / 60000UL);
}

bool resetCountdownText(uint8_t index, char* out, size_t size) {
  int32_t minutes = resetMinutesLeft(index);
  if (minutes < 0) return false;
  if (minutes >= 1440) {
    snprintf(out, size, "%uD%02u", static_cast<unsigned>(minutes / 1440),
             static_cast<unsigned>((minutes % 1440) / 60));
  } else if (minutes >= 60) {
    snprintf(out, size, "%uH%02u", static_cast<unsigned>(minutes / 60),
             static_cast<unsigned>(minutes % 60));
  } else {
    snprintf(out, size, "%uM", static_cast<unsigned>(minutes));
  }
  return true;
}

void drawUsageSummary() {
  display->fillRect(0, 43, SCREEN_W, 35, BLACK);
  display->setTextSize(1);
  if (usage.valid) {
    display->setTextColor(CLAUDE_COLOR);
    display->setCursor(22, 48);
    display->printf("C NEXT C%u", lowestAccount(usage.claude) + 1);
    display->setTextColor(cfg.accent);
    display->setCursor(132, 48);
    display->printf("X NEXT X%u", lowestAccount(usage.codex) + 1);
  } else {
    centered("WAITING FOR USAGE", 47, 1, GRAY);
  }

  uint32_t ageMinutes = usage.valid ? (millis() - lastUsageMillis) / 60000UL : 0;
  display->setTextColor(!usage.valid || ageMinutes >= 5 ? RED : GREEN);
  display->setCursor(14, 66);
  if (!usage.valid) {
    display->print("MAC OFFLINE");
  } else if (ageMinutes == 0) {
    display->print("SYNC NOW");
  } else {
    display->printf("SYNC %um", static_cast<unsigned>(ageMinutes));
  }

  if (usage.valid) {
    // Countdown until the most relevant account's quota resets: the active
    // account when it belongs to the column, otherwise the fullest account.
    char countdown[12];
    uint8_t claudeIndex = activeAccount < 3 ? activeAccount
                                            : highestAccount(usage.claude);
    if (resetCountdownText(claudeIndex, countdown, sizeof(countdown))) {
      display->setTextColor(CLAUDE_COLOR);
      display->setCursor(96, 66);
      display->printf("C%u %s", claudeIndex + 1, countdown);
    }
    uint8_t codexIndex = (activeAccount >= 3 && activeAccount < 6)
                             ? activeAccount - 3
                             : highestAccount(usage.codex);
    if (resetCountdownText(codexIndex + 3, countdown, sizeof(countdown))) {
      display->setTextColor(cfg.accent);
      display->setCursor(170, 66);
      display->printf("X%u %s", codexIndex + 1, countdown);
    }
  }
  lastSyncMinuteDrawn = millis() / 60000UL;
}

void drawUsageRegion() {
  // This is the only area refreshed for incoming host usage updates. It keeps
  // periodic USB polling from flashing the title bar or full panel.
  display->fillRect(0, 43, SCREEN_W, 197, BLACK);
  drawUsageSummary();
  display->drawFastHLine(12, 79, 216, DARK);
  display->setTextSize(1);
  display->setTextColor(CLAUDE_COLOR);
  display->setCursor(14, 86);
  display->print("CLAUDE");

  const char* claudeAccounts[] = {"C1", "C2", "C3"};
  const char* codexAccounts[] = {"X1", "X2", "X3"};
  for (uint8_t i = 0; i < 3; ++i) {
    int y = 101 + i * 17;
    uint8_t claudePrevious = usageHistoryCount > 1 ?
        usageHistory[i][usageHistoryCount - 2] : usage.claude[i];
    uint8_t codexPrevious = usageHistoryCount > 1 ?
        usageHistory[i + 3][usageHistoryCount - 2] : usage.codex[i];
    drawUsageBar(claudeAccounts[i], usage.claude[i], claudePrevious, y,
                 CLAUDE_COLOR, activeAccount == i);
    drawUsageBar(codexAccounts[i], usage.codex[i], codexPrevious, 169 + i * 17,
                 cfg.accent, activeAccount == i + 3);
  }
  display->setTextSize(1);
  display->setTextColor(GRAY);
  display->setCursor(14, 154);
  display->print("CODEX WEEKLY");
}

uint16_t currentMinuteOfDay() {
  if (!timeWasSet) return 0;
  return (baseMinuteOfDay + (millis() - baseTimeMillis) / 60000UL) % 1440;
}

const char* faceMoodName(FaceMood mood) {
  switch (mood) {
    case FaceMood::AUTO: return "auto";
    case FaceMood::NEUTRAL: return "neutral";
    case FaceMood::HAPPY: return "happy";
    case FaceMood::FOCUS: return "focus";
    case FaceMood::CURIOUS: return "curious";
    case FaceMood::SLEEPY: return "sleepy";
    case FaceMood::ALERT: return "alert";
    case FaceMood::CELEBRATE: return "celebrate";
  }
  return "auto";
}

bool parseFaceMood(const char* input, FaceMood& output) {
  for (uint8_t value = static_cast<uint8_t>(FaceMood::AUTO);
       value <= static_cast<uint8_t>(FaceMood::CELEBRATE); ++value) {
    FaceMood candidate = static_cast<FaceMood>(value);
    if (!strcasecmp(input, faceMoodName(candidate))) {
      output = candidate;
      return true;
    }
  }
  return false;
}

FaceMood resolvedFaceMood() {
  if (faceMood != FaceMood::AUTO) return faceMood;
  if (autoFaceMoodSet) return autoFaceMood;
  if (!timeWasSet) return FaceMood::CURIOUS;
  uint16_t minute = currentMinuteOfDay();
  if (minute >= 23 * 60 || minute < 7 * 60) return FaceMood::SLEEPY;
  if (minute < 9 * 60) return FaceMood::HAPPY;
  if (minute < 18 * 60) return FaceMood::FOCUS;
  return FaceMood::NEUTRAL;
}

uint16_t dimColor(uint16_t color, uint8_t numerator, uint8_t denominator) {
  uint8_t red = ((color >> 11) & 0x1F) * numerator / denominator;
  uint8_t green = ((color >> 5) & 0x3F) * numerator / denominator;
  uint8_t blue = (color & 0x1F) * numerator / denominator;
  return static_cast<uint16_t>((red << 11) | (green << 5) | blue);
}

void drawLedEye(int x, int y, int width, int height, uint16_t color) {
  int glowRadius = max(2, min(width + 8, height + 8) / 4);
  int eyeRadius = max(2, min(width, height) / 3);
  display->fillRoundRect(x - 4, y - 4, width + 8, height + 8,
                         glowRadius, dimColor(color, 1, 4));
  display->fillRoundRect(x, y, width, height, eyeRadius, color);
}

void drawThickLine(int x0, int y0, int x1, int y1, uint8_t thickness,
                   uint16_t color) {
  int offset = thickness / 2;
  for (int delta = -offset; delta <= offset; ++delta) {
    display->drawLine(x0, y0 + delta, x1, y1 + delta, color);
  }
}

void drawHappyEye(int x, int y, int width, uint16_t color) {
  int middle = x + width / 2;
  drawThickLine(x, y + 10, middle, y, 7, dimColor(color, 1, 4));
  drawThickLine(middle, y, x + width, y + 10, 7, dimColor(color, 1, 4));
  drawThickLine(x + 2, y + 9, middle, y + 2, 4, color);
  drawThickLine(middle, y + 2, x + width - 2, y + 9, 4, color);
}

void drawFaceEyeRegion() {
  constexpr int REGION_X = 28;
  constexpr int REGION_Y = 72;
  constexpr int REGION_W = 184;
  constexpr int REGION_H = 100;
  display->fillRect(REGION_X, REGION_Y, REGION_W, REGION_H, BLACK);

  FaceMood mood = resolvedFaceMood();
  uint32_t now = millis();
  int yOffset = 0;
  uint16_t color = cfg.accent;
  if (mood == FaceMood::ALERT && (now / 350UL) % 2) {
    color = dimColor(color, 2, 3);
  } else if (mood == FaceMood::CELEBRATE) {
    static const int8_t bounce[] = {0, -4, 0, 2};
    yOffset = bounce[(now / 180UL) % 4];
  }

  if (faceBlinkPhase) {
    int height = faceBlinkPhase == 2 ? 4 : 12;
    int y = 116 - height / 2;
    drawLedEye(51 + faceLookOffset, y, 50, height, color);
    drawLedEye(139 + faceLookOffset, y, 50, height, color);
    lastRenderedFaceMood = mood;
    faceDirty = false;
    return;
  }

  switch (mood) {
    case FaceMood::HAPPY:
    case FaceMood::CELEBRATE:
      drawHappyEye(52, 104 + yOffset, 48, color);
      drawHappyEye(140, 104 + yOffset, 48, color);
      break;
    case FaceMood::FOCUS:
      drawLedEye(51 + faceLookOffset, 99, 50, 34, color);
      drawLedEye(139 + faceLookOffset, 99, 50, 34, color);
      // Diagonal upper lids create a focused expression without eyebrows.
      display->fillTriangle(47 + faceLookOffset, 95,
                            105 + faceLookOffset, 95,
                            105 + faceLookOffset, 111, BLACK);
      display->fillTriangle(135 + faceLookOffset, 95,
                            193 + faceLookOffset, 95,
                            135 + faceLookOffset, 111, BLACK);
      break;
    case FaceMood::CURIOUS:
      drawLedEye(53 + faceLookOffset, 103, 44, 30, color);
      drawLedEye(136 + faceLookOffset, 96, 54, 42, color);
      break;
    case FaceMood::SLEEPY:
      drawLedEye(51 + faceLookOffset, 111, 50, 13, color);
      drawLedEye(139 + faceLookOffset, 111, 50, 13, color);
      break;
    case FaceMood::ALERT:
      drawLedEye(53, 92, 46, 50, color);
      drawLedEye(141, 92, 46, 50, color);
      break;
    case FaceMood::AUTO:
    case FaceMood::NEUTRAL:
      drawLedEye(51 + faceLookOffset, 99, 50, 34, color);
      drawLedEye(139 + faceLookOffset, 99, 50, 34, color);
      break;
  }
  lastRenderedFaceMood = mood;
  faceDirty = false;
}

void resetFaceAnimation() {
  uint32_t now = millis();
  faceBlinking = false;
  faceBlinkPhase = 0;
  faceLookOffset = 0;
  nextFaceBlinkMillis = now + random(3500, 9001);
  nextFaceLookMillis = now + random(2200, 5201);
  lastFaceEffectFrame = UINT32_MAX;
  faceDirty = true;
}

void drawFaceScreen() {
  if (!display) return;
  testPatternActive = false;
  display->fillScreen(BLACK);
  resetFaceAnimation();
  drawFaceEyeRegion();
  screenDirty = false;
  usageDirty = false;
}

bool millisReached(uint32_t now, uint32_t target) {
  return static_cast<int32_t>(now - target) >= 0;
}

void serviceFaceAnimation() {
  if (!display || testPatternActive || screenMode != ScreenMode::FACE) return;
  uint32_t now = millis();
  FaceMood mood = resolvedFaceMood();
  if (mood != lastRenderedFaceMood) faceDirty = true;

  if (faceBlinking) {
    uint32_t elapsed = now - faceBlinkStartMillis;
    uint8_t phase = elapsed < 45 ? 1 : elapsed < 90 ? 2 : elapsed < 135 ? 1 : 0;
    if (phase != faceBlinkPhase) {
      faceBlinkPhase = phase;
      faceDirty = true;
    }
    if (elapsed >= 160) {
      faceBlinking = false;
      faceBlinkPhase = 0;
      // Occasional double-blink keeps the otherwise minimal face alive.
      nextFaceBlinkMillis = now + (random(4) == 0 ? 140 : random(3500, 9001));
      faceDirty = true;
    }
  } else if (millisReached(now, nextFaceBlinkMillis)) {
    faceBlinking = true;
    faceBlinkStartMillis = now;
    faceBlinkPhase = 1;
    faceDirty = true;
  }

  if (!faceBlinking && millisReached(now, nextFaceLookMillis)) {
    if (faceLookOffset) {
      faceLookOffset = 0;
      nextFaceLookMillis = now + random(2200, 5201);
    } else {
      faceLookOffset = random(2) ? 5 : -5;
      nextFaceLookMillis = now + random(500, 1101);
    }
    faceDirty = true;
  }

  if (mood == FaceMood::ALERT || mood == FaceMood::CELEBRATE) {
    uint32_t interval = mood == FaceMood::ALERT ? 350UL : 180UL;
    uint32_t frame = now / interval;
    if (frame != lastFaceEffectFrame) {
      lastFaceEffectFrame = frame;
      faceDirty = true;
    }
  } else {
    lastFaceEffectFrame = UINT32_MAX;
  }

  if (faceDirty) drawFaceEyeRegion();
}

void drawClockTime() {
  char timeText[9];
  uint16_t minute = currentMinuteOfDay();
  snprintf(timeText, sizeof(timeText), "%02u:%02u", minute / 60, minute % 60);
  display->fillRect(0, 78, SCREEN_W, 72, BLACK);
  centered(timeText, 91, 5, WHITE);
}

void drawClockScreen() {
  if (!display) return;
  testPatternActive = false;
  display->fillScreen(BLACK);
  display->fillRect(0, 0, SCREEN_W, 6, cfg.accent);
  clippedLabel(cfg.title, 12, 17, 2, WHITE, 18);
  display->setTextSize(1);
  display->setTextColor(cfg.accent);
  display->setCursor(86, 58);
  display->print("LOCAL TIME");
  drawClockTime();
  centered(cfg.city, 181, 2, GRAY);
  display->setTextColor(GREEN);
  display->setCursor(91, 218);
  display->print("USB CONNECTED");
  lastSyncMinuteDrawn = millis() / 60000UL;
  screenDirty = false;
  usageDirty = false;
}

void drawThaiScreen() {
  if (!display) return;
  testPatternActive = false;
  display->fillScreen(BLACK);
  display->fillRect(0, 0, SCREEN_W, 6, cfg.accent);
  centered("THAI FONT TEST", 25, 2, WHITE);
  centered("HELLO", 61, 1, GRAY);
  const int x = (SCREEN_W - THAI_GREETING_WIDTH) / 2;
  const int y = 89;
  display->drawBitmap(x, y, THAI_GREETING_BITMAP,
                      THAI_GREETING_WIDTH, THAI_GREETING_HEIGHT, cfg.accent);
  centered("USB-ONLY FIRMWARE", 188, 1, GRAY);
  display->setTextColor(GREEN);
  display->setCursor(91, 218);
  display->print("USB CONNECTED");
  screenDirty = false;
  usageDirty = false;
}

String galleryPath(uint8_t slot) {
  return String("/gallery-") + slot + ".raw";
}

bool galleryExists(uint8_t slot) {
  File image = LittleFS.open(galleryPath(slot), "r");
  bool valid = image && image.size() == GALLERY_BYTES;
  if (image) image.close();
  return valid;
}

bool advanceGallery() {
  for (uint8_t step = 1; step <= GALLERY_SLOTS; ++step) {
    uint8_t candidate = (gallerySlot + step) % GALLERY_SLOTS;
    if (galleryExists(candidate)) {
      gallerySlot = candidate;
      return true;
    }
  }
  return false;
}

void drawGalleryScreen() {
  if (!display) return;
  testPatternActive = false;
  display->fillScreen(BLACK);
  File image = LittleFS.open(galleryPath(gallerySlot), "r");
  if (!image || image.size() != GALLERY_BYTES) {
    centered("NO IMAGE IN SLOT", 92, 2, GRAY);
    centered("UPLOAD FROM MAC GUI", 125, 1, WHITE);
  } else {
    uint16_t row[SCREEN_W];
    for (int y = 0; y < SCREEN_H; ++y) {
      if (image.read(reinterpret_cast<uint8_t*>(row), sizeof(row)) != sizeof(row)) break;
      display->draw16bitRGBBitmap(0, y, row, SCREEN_W, 1);
    }
    image.close();
  }
  screenDirty = false;
  usageDirty = false;
}

void drawUsageScreen() {
  if (!display) return;
  testPatternActive = false;
  display->fillScreen(BLACK);

  display->fillRect(0, 0, SCREEN_W, 6, cfg.accent);
  clippedLabel(cfg.title, 12, 17, 2, WHITE, 18);

  display->fillCircle(220, 24, 5, GREEN);
  display->setTextSize(1);
  display->setTextColor(GRAY);
  display->setCursor(190, 37);
  display->print("USB");

  drawUsageRegion();

  screenDirty = false;
  usageDirty = false;
}

void formatUsd(uint32_t priceCents, char* output, size_t size) {
  char digits[12];
  snprintf(digits, sizeof(digits), "%lu",
           static_cast<unsigned long>(priceCents / 100));
  size_t length = strlen(digits);
  size_t position = 0;
  if (position + 1 < size) output[position++] = '$';
  for (size_t i = 0; i < length && position + 1 < size; ++i) {
    if (i && (length - i) % 3 == 0 && position + 1 < size) {
      output[position++] = ',';
    }
    output[position++] = digits[i];
  }
  output[position] = '\0';
}

int cryptoY(uint8_t normalized) {
  constexpr int CHART_TOP = 116;
  constexpr int CHART_HEIGHT = 88;
  return CHART_TOP + CHART_HEIGHT - normalized * CHART_HEIGHT / 100;
}

void drawCryptoRegion() {
  display->fillRect(0, 43, SCREEN_W, 197, BLACK);
  if (!crypto.valid) {
    centered("WAITING FOR BTC", 92, 2, GRAY);
    centered("MAC DATA REQUIRED", 126, 1, WHITE);
    cryptoDirty = false;
    return;
  }

  char price[16];
  formatUsd(crypto.priceCents, price, sizeof(price));
  centered(price, 49, 3, WHITE);

  int32_t change = crypto.changeBps;
  uint32_t magnitude = change < 0 ? -change : change;
  char changeText[18];
  snprintf(changeText, sizeof(changeText), "24H %c%lu.%02lu%%",
           change >= 0 ? '+' : '-', static_cast<unsigned long>(magnitude / 100),
           static_cast<unsigned long>(magnitude % 100));
  centered(changeText, 85, 1, change >= 0 ? GREEN : RED);
  display->drawFastHLine(12, 105, 216, DARK);

  for (uint8_t i = 0; i < BTC_CANDLES; ++i) {
    uint8_t opened = crypto.candles[i][0];
    uint8_t high = crypto.candles[i][1];
    uint8_t low = crypto.candles[i][2];
    uint8_t closed = crypto.candles[i][3];
    int x = 12 + i * 9;
    int highY = cryptoY(high);
    int lowY = cryptoY(low);
    int openY = cryptoY(opened);
    int closeY = cryptoY(closed);
    uint16_t color = closed >= opened ? GREEN : RED;
    display->drawFastVLine(x + 3, highY, max(1, lowY - highY + 1), GRAY);
    int bodyTop = min(openY, closeY);
    int bodyHeight = max(2, abs(closeY - openY) + 1);
    display->fillRect(x + 1, bodyTop, 5, bodyHeight, color);
  }
  centered("1H CANDLES  |  LAST 24H", 218, 1, GRAY);
  cryptoDirty = false;
}

void drawCryptoScreen() {
  if (!display) return;
  testPatternActive = false;
  display->fillScreen(BLACK);
  display->fillRect(0, 0, SCREEN_W, 6, cfg.accent);
  clippedLabel("BTC / USD", 12, 17, 2, WHITE, 18);
  display->fillCircle(220, 24, 5, crypto.valid ? GREEN : RED);
  drawCryptoRegion();
  screenDirty = false;
  usageDirty = false;
}

void drawScreen() {
  if (screenMode == ScreenMode::FACE) drawFaceScreen();
  else if (screenMode == ScreenMode::BTC) drawCryptoScreen();
  else if (screenMode == ScreenMode::CLOCK) drawClockScreen();
  else if (screenMode == ScreenMode::THAI) drawThaiScreen();
  else if (screenMode == ScreenMode::GALLERY) drawGalleryScreen();
  else drawUsageScreen();
}

void drawAlertScreen() {
  if (!display) return;
  testPatternActive = false;
  const ReminderSlot& reminder = reminders.slots[alertReminderSlot];
  char timeText[9];
  snprintf(timeText, sizeof(timeText), "%02u:%02u",
           reminder.minuteOfDay / 60, reminder.minuteOfDay % 60);
  display->fillScreen(BLACK);
  display->fillRect(0, 0, SCREEN_W, 34, cfg.accent);
  centered("REMINDER", 9, 2, BLACK);
  centered("!", 55, 5, cfg.accent);
  centered(reminder.label, 123, 2, WHITE);
  centered(timeText, 164, 3, cfg.accent);
  centered("BACK IN 60S", 218, 1, GRAY);
  screenDirty = false;
  usageDirty = false;
}

void cancelAlert() {
  if (!alertActive) return;
  alertActive = false;
  screenDirty = true;
}

void fireReminder(uint8_t slot) {
  if (alertActive) return;
  reminderFiredMask |= 1 << slot;
  alertReturnMode = screenMode;
  alertReminderSlot = slot;
  alertActive = true;
  alertStartMillis = millis();
  drawAlertScreen();
}

void checkReminders() {
  if (!timeWasSet || alertActive) return;
  uint16_t minute = currentMinuteOfDay();
  if (minute == lastReminderCheckMinute) return;
  if (lastReminderCheckMinute != EMPTY_REMINDER &&
      minute < lastReminderCheckMinute) {
    reminderFiredMask = 0;
  }
  lastReminderCheckMinute = minute;
  for (uint8_t i = 0; i < REMINDER_SLOTS; ++i) {
    const ReminderSlot& slot = reminders.slots[i];
    if (!slot.enabled || slot.minuteOfDay >= 1440 ||
        (reminderFiredMask & (1 << i))) continue;
    uint16_t minutesAgo = (minute + 1440 - slot.minuteOfDay) % 1440;
    if (minutesAgo <= 2) fireReminder(i);
  }
}

void drawTestPattern() {
  display->fillRect(0, 0, 120, 120, RED);
  display->fillRect(120, 0, 120, 120, GREEN);
  display->fillRect(0, 120, 120, 120, BLUE);
  display->fillRect(120, 120, 120, 120, WHITE);
  display->setTextColor(BLACK);
  display->setTextSize(2);
  display->setCursor(137, 175);
  display->print("TEST");
  testPatternActive = true;
  screenDirty = false;
  usageDirty = false;
}

bool parseBool(const char* text, bool& output) {
  if (!strcasecmp(text, "1") || !strcasecmp(text, "on") ||
      !strcasecmp(text, "true")) {
    output = true;
    return true;
  }
  if (!strcasecmp(text, "0") || !strcasecmp(text, "off") ||
      !strcasecmp(text, "false")) {
    output = false;
    return true;
  }
  return false;
}

bool parseHexColor(const char* text, uint16_t& output) {
  if (*text == '#') ++text;
  if (strlen(text) != 6) return false;
  char* end = nullptr;
  unsigned long rgb = strtoul(text, &end, 16);
  if (!end || *end) return false;
  uint8_t r = (rgb >> 16) & 0xFF;
  uint8_t g = (rgb >> 8) & 0xFF;
  uint8_t b = rgb & 0xFF;
  output = static_cast<uint16_t>(((r & 0xF8) << 8) |
                                 ((g & 0xFC) << 3) | (b >> 3));
  return true;
}

void printStatus() {
  char color[8];
  uint8_t r = ((cfg.accent >> 11) & 0x1F) * 255 / 31;
  uint8_t g = ((cfg.accent >> 5) & 0x3F) * 255 / 63;
  uint8_t b = (cfg.accent & 0x1F) * 255 / 31;
  snprintf(color, sizeof(color), "%02X%02X%02X", r, g, b);
  Serial.print(F("STATUS title=\"")); Serial.print(cfg.title);
  Serial.print(F("\" city=\"")); Serial.print(cfg.city);
  Serial.print(F("\" brightness=")); Serial.print(cfg.brightness);
  Serial.print(F(" rotation=")); Serial.print(cfg.rotation);
  Serial.print(F(" blinvert=")); Serial.print(cfg.backlightInverted);
  Serial.print(F(" accent=#")); Serial.print(color);
  Serial.print(F(" claude="));
  Serial.printf("%u,%u,%u", usage.claude[0], usage.claude[1], usage.claude[2]);
  Serial.print(F(" codex_weekly="));
  Serial.printf("%u,%u,%u", usage.codex[0], usage.codex[1], usage.codex[2]);
  Serial.print(F(" active="));
  if (activeAccount == 0xFF) Serial.print(F("none"));
  else Serial.printf("%c%u", activeAccount < 3 ? 'C' : 'X',
                     activeAccount % 3 + 1);
  Serial.print(F(" reset_minutes="));
  for (uint8_t i = 0; i < 6; ++i) {
    Serial.printf("%s%ld", i ? "," : "", static_cast<long>(resetMinutesLeft(i)));
  }
  uint8_t reminderCount = 0;
  for (uint8_t i = 0; i < REMINDER_SLOTS; ++i) {
    if (reminders.slots[i].enabled) ++reminderCount;
  }
  Serial.print(F(" reminders=")); Serial.print(reminderCount);
  Serial.print(F(" btc_usd_cents="));
  if (crypto.valid) Serial.print(crypto.priceCents);
  else Serial.print(F("unknown"));
  Serial.print(F(" face_mood=")); Serial.print(faceMoodName(faceMood));
  Serial.print(F(" auto_emotion="));
  if (autoFaceMoodSet) Serial.print(faceMoodName(autoFaceMood));
  else Serial.print(F("time"));
  Serial.print(F(" screen="));
  if (screenMode == ScreenMode::FACE) Serial.println(F("face"));
  else if (screenMode == ScreenMode::BTC) Serial.println(F("btc"));
  else if (screenMode == ScreenMode::CLOCK) Serial.println(F("clock"));
  else if (screenMode == ScreenMode::THAI) Serial.println(F("thai"));
  else if (screenMode == ScreenMode::GALLERY) Serial.println(F("gallery"));
  else Serial.println(F("usage"));
}

void printHelp() {
  Serial.println(F("COMMANDS"));
  Serial.println(F("  STATUS"));
  Serial.println(F("  SET TITLE <text>"));
  Serial.println(F("  SET CITY <text>"));
  Serial.println(F("  SET BRIGHTNESS <0-100>"));
  Serial.println(F("  SET ROTATION <0-3>"));
  Serial.println(F("  SET BLINVERT <on|off>"));
  Serial.println(F("  SET ACCENT <#RRGGBB>"));
  Serial.println(F("  SET TIME <HH:MM>"));
  Serial.println(F("  USAGE <c1> <c2> <c3> <x1> <x2> <x3> [previous c1..x3]"));
  Serial.println(F("  RESETS <c1> <c2> <c3> <x1> <x2> <x3>  (minutes until reset, -1 unknown)"));
  Serial.println(F("  ACTIVE <C1|C2|C3|X1|X2|X3|OFF>"));
  Serial.println(F("  BTC <price-cents> <24h-change-bps> <24 normalized OHLC candles>"));
  Serial.println(F("  SCREEN <FACE|USAGE|BTC|CLOCK|THAI>"));
  Serial.println(F("  FACE <AUTO|NEUTRAL|HAPPY|FOCUS|CURIOUS|SLEEPY|ALERT|CELEBRATE>"));
  Serial.println(F("  EMOTION <NEUTRAL|HAPPY|FOCUS|CURIOUS|SLEEPY|ALERT|CELEBRATE|CLEAR>"));
  Serial.println(F("  GALLERY BEGIN <slot 0-6> <115200>"));
  Serial.println(F("  GALLERY END <fnv-hash> | SHOW <slot> | DELETE <slot>"));
  Serial.println(F("  REMIND SET <slot 0-7> <HH:MM> <ASCII text up to 20 chars>"));
  Serial.println(F("  REMIND DEL <slot 0-7>"));
  Serial.println(F("  REMIND LIST"));
  Serial.println(F("  SHOW"));
  Serial.println(F("  TEST"));
  Serial.println(F("  SAVE"));
  Serial.println(F("  RESETCFG"));
  Serial.println(F("  REBOOT"));
}

int8_t hexValue(char value) {
  if (value >= '0' && value <= '9') return value - '0';
  value = toupper(value);
  if (value >= 'A' && value <= 'F') return value - 'A' + 10;
  return -1;
}

bool parseBtc(char* input) {
  char* priceText = strtok(input, " ");
  char* changeText = strtok(nullptr, " ");
  char* candlesText = strtok(nullptr, " ");
  if (!priceText || !changeText || !candlesText || strtok(nullptr, " ") ||
      strlen(candlesText) != BTC_HEX_LENGTH) return false;

  char* end = nullptr;
  errno = 0;
  unsigned long price = strtoul(priceText, &end, 10);
  if (!isdigit(priceText[0]) || errno == ERANGE || !end || *end || price == 0) {
    return false;
  }
  end = nullptr;
  long change = strtol(changeText, &end, 10);
  if (!end || *end || change < -32768 || change > 32767) return false;

  uint8_t decoded[BTC_CANDLES][4];
  uint8_t* bytes = reinterpret_cast<uint8_t*>(decoded);
  for (size_t i = 0; i < BTC_CANDLES * 4; ++i) {
    int8_t highNibble = hexValue(candlesText[i * 2]);
    int8_t lowNibble = hexValue(candlesText[i * 2 + 1]);
    if (highNibble < 0 || lowNibble < 0) return false;
    bytes[i] = highNibble * 16 + lowNibble;
    if (bytes[i] > 100) return false;
  }
  for (uint8_t i = 0; i < BTC_CANDLES; ++i) {
    uint8_t opened = decoded[i][0];
    uint8_t high = decoded[i][1];
    uint8_t low = decoded[i][2];
    uint8_t closed = decoded[i][3];
    if (low > high || opened < low || opened > high ||
        closed < low || closed > high) return false;
  }
  crypto.priceCents = static_cast<uint32_t>(price);
  crypto.changeBps = static_cast<int16_t>(change);
  memcpy(crypto.candles, decoded, sizeof(decoded));
  crypto.valid = true;
  cryptoDirty = true;
  return true;
}

bool parseReminderSlot(const char* input, uint8_t& slot) {
  char* end = nullptr;
  long value = strtol(input, &end, 10);
  if (!end || *end || value < 0 || value >= REMINDER_SLOTS) return false;
  slot = static_cast<uint8_t>(value);
  return true;
}

void printReminders() {
  for (uint8_t i = 0; i < REMINDER_SLOTS; ++i) {
    const ReminderSlot& slot = reminders.slots[i];
    Serial.printf("REMIND %u ", i);
    if (!slot.enabled || slot.minuteOfDay >= 1440) {
      Serial.println(F("off"));
    } else {
      Serial.printf("%02u:%02u %s\n", slot.minuteOfDay / 60,
                    slot.minuteOfDay % 60, slot.label);
    }
  }
}

void handleRemind(char* input) {
  char* action = strtok(input, " ");
  if (!action) {
    Serial.println(F("ERR reminder command"));
    return;
  }
  if (!strcasecmp(action, "LIST")) {
    if (strtok(nullptr, " ")) Serial.println(F("ERR reminder list takes no arguments"));
    else printReminders();
    return;
  }

  char* slotText = strtok(nullptr, " ");
  uint8_t slot = 0;
  if (!slotText || !parseReminderSlot(slotText, slot)) {
    Serial.println(F("ERR reminder slot must be 0-7"));
    return;
  }
  if (!strcasecmp(action, "DEL")) {
    if (strtok(nullptr, " ")) {
      Serial.println(F("ERR reminder delete takes one slot"));
      return;
    }
    reminders.slots[slot].minuteOfDay = EMPTY_REMINDER;
    reminders.slots[slot].label[0] = '\0';
    reminders.slots[slot].enabled = 0;
    reminderFiredMask &= ~(1 << slot);
    Serial.println(saveReminders() ? F("OK") : F("ERR reminder save failed"));
    return;
  }
  if (strcasecmp(action, "SET")) {
    Serial.println(F("ERR reminder action must be SET, DEL, or LIST"));
    return;
  }

  char* timeText = strtok(nullptr, " ");
  char* label = strtok(nullptr, "");
  int hour = -1;
  int minute = -1;
  char extra = 0;
  if (!timeText || sscanf(timeText, "%d:%d%c", &hour, &minute, &extra) != 2 ||
      hour < 0 || hour > 23 || minute < 0 || minute > 59) {
    Serial.println(F("ERR reminder time must be HH:MM"));
    return;
  }
  while (label && *label == ' ') ++label;
  size_t labelLength = label ? strlen(label) : 0;
  if (!labelLength || labelLength > 20) {
    Serial.println(F("ERR reminder label must be 1-20 ASCII characters"));
    return;
  }
  for (size_t i = 0; i < labelLength; ++i) {
    if (static_cast<uint8_t>(label[i]) < 32 || static_cast<uint8_t>(label[i]) > 126) {
      Serial.println(F("ERR reminder label must use printable ASCII"));
      return;
    }
  }
  ReminderSlot& reminder = reminders.slots[slot];
  reminder.minuteOfDay = hour * 60 + minute;
  copyText(reminder.label, sizeof(reminder.label), label);
  reminder.enabled = 1;
  reminderFiredMask &= ~(1 << slot);
  Serial.println(saveReminders() ? F("OK") : F("ERR reminder save failed"));
}

bool parseUsage(char* input) {
  uint8_t values[12];
  uint8_t count = 0;
  for (; count < 12; ++count) {
    char* token = strtok(count == 0 ? input : nullptr, " ");
    if (!token) break;
    char* end = nullptr;
    long value = strtol(token, &end, 10);
    if (!end || *end || value < 0 || value > 100) return false;
    values[count] = static_cast<uint8_t>(value);
  }
  if ((count != 6 && count != 12) || strtok(nullptr, " ")) return false;
  if (count == 12) {
    usageHistoryCount = 2;
    for (uint8_t i = 0; i < 6; ++i) {
      usageHistory[i][0] = values[i + 6];
      usageHistory[i][1] = values[i];
    }
  } else for (uint8_t i = 0; i < 6; ++i) {
    uint8_t value = values[i];
    if (usageHistoryCount < 12) {
      usageHistory[i][usageHistoryCount] = value;
    } else {
      memmove(usageHistory[i], usageHistory[i] + 1, 11);
      usageHistory[i][11] = value;
    }
  }
  if (count == 6 && usageHistoryCount < 12) ++usageHistoryCount;
  memcpy(usage.claude, values, sizeof(usage.claude));
  memcpy(usage.codex, values + 3, sizeof(usage.codex));
  usage.valid = true;
  lastUsageMillis = millis();
  usageDirty = true;
  return true;
}

bool parseResets(char* input) {
  long values[6];
  for (uint8_t i = 0; i < 6; ++i) {
    char* token = strtok(i == 0 ? input : nullptr, " ");
    if (!token) return false;
    char* end = nullptr;
    long value = strtol(token, &end, 10);
    if (!end || *end || value < -1 || value > 65535) return false;
    values[i] = value;
  }
  if (strtok(nullptr, " ")) return false;
  resetBaseMillis = millis();
  for (uint8_t i = 0; i < 6; ++i) {
    if (values[i] < 0) {
      resetKnownMask &= ~(1 << i);
    } else {
      resetDurationMillis[i] = static_cast<uint32_t>(values[i]) * 60000UL;
      resetKnownMask |= 1 << i;
    }
  }
  usageDirty = true;
  return true;
}

bool parseActive(const char* input) {
  if (!strcasecmp(input, "OFF")) {
    activeAccount = 0xFF;
    return true;
  }
  if (strlen(input) != 2 || (toupper(input[0]) != 'C' && toupper(input[0]) != 'X') ||
      input[1] < '1' || input[1] > '3') return false;
  activeAccount = (toupper(input[0]) == 'C' ? 0 : 3) + input[1] - '1';
  return true;
}

bool parseScreen(const char* input) {
  if (!strcasecmp(input, "USAGE")) {
    cancelAlert();
    setDisplayPersistence(ScreenMode::USAGE);
    screenMode = ScreenMode::USAGE;
    galleryPlaying = false;
    saveGalleryPlayback();
    return true;
  }
  if (!strcasecmp(input, "BTC")) {
    cancelAlert();
    setDisplayPersistence(ScreenMode::BTC);
    screenMode = ScreenMode::BTC;
    galleryPlaying = false;
    saveGalleryPlayback();
    return true;
  }
  if (!strcasecmp(input, "CLOCK")) {
    cancelAlert();
    setDisplayPersistence(ScreenMode::CLOCK);
    screenMode = ScreenMode::CLOCK;
    galleryPlaying = false;
    saveGalleryPlayback();
    return true;
  }
  if (!strcasecmp(input, "THAI")) {
    cancelAlert();
    setDisplayPersistence(ScreenMode::THAI);
    screenMode = ScreenMode::THAI;
    galleryPlaying = false;
    saveGalleryPlayback();
    return true;
  }
  if (!strcasecmp(input, "FACE")) {
    cancelAlert();
    setDisplayPersistence(ScreenMode::FACE);
    screenMode = ScreenMode::FACE;
    galleryPlaying = false;
    saveGalleryPlayback();
    return true;
  }
  return false;
}

bool setFace(const char* input) {
  FaceMood mood;
  if (!parseFaceMood(input, mood)) return false;
  cancelAlert();
  persistFaceMood(mood);
  screenMode = ScreenMode::FACE;
  galleryPlaying = false;
  saveGalleryPlayback();
  return true;
}

bool setAutoEmotion(const char* input) {
  if (!strcasecmp(input, "CLEAR")) {
    autoFaceMoodSet = false;
  } else {
    FaceMood mood;
    if (!parseFaceMood(input, mood) || mood == FaceMood::AUTO) return false;
    autoFaceMood = mood;
    autoFaceMoodSet = true;
  }
  if (screenMode == ScreenMode::FACE && faceMood == FaceMood::AUTO) {
    faceDirty = true;
  }
  return true;
}

bool parseGallerySlot(const char* input, uint8_t& slot) {
  char* end = nullptr;
  long value = strtol(input, &end, 10);
  if (!end || *end || value < 0 || value >= GALLERY_SLOTS) return false;
  slot = static_cast<uint8_t>(value);
  return true;
}

void handleGallery(char* input) {
  char* action = strtok(input, " ");
  char* first = strtok(nullptr, " ");
  char* second = strtok(nullptr, " ");
  char* extra = strtok(nullptr, " ");
  if (!action || extra) {
    Serial.println(F("ERR gallery command"));
    return;
  }
  uint8_t slot = 0;
  if (!strcasecmp(action, "BEGIN")) {
    char* end = nullptr;
    long size = second ? strtol(second, &end, 10) : -1;
    if (!first || !parseGallerySlot(first, slot) || !second || !end || *end ||
        size != GALLERY_BYTES) {
      Serial.println(F("ERR gallery begin requires slot and 115200 bytes"));
      return;
    }
    if (galleryUpload) galleryUpload.close();
    LittleFS.remove(GALLERY_UPLOAD_PATH);
    galleryUploadComplete = false;
    galleryUpload = LittleFS.open(GALLERY_UPLOAD_PATH, "w");
    if (!galleryUpload) {
      Serial.println(F("ERR gallery storage unavailable"));
      return;
    }
    galleryUploadSlot = slot;
    galleryRemaining = GALLERY_BYTES;
    galleryHash = 2166136261UL;
    Serial.println(F("OK gallery ready"));
  } else if (!strcasecmp(action, "END")) {
    if (!first || second || strlen(first) != 8) {
      Serial.println(F("ERR gallery checksum"));
      return;
    }
    char* end = nullptr;
    errno = 0;
    unsigned long expected = strtoul(first, &end, 16);
    if (errno == ERANGE || !end || *end || galleryRemaining || galleryUpload ||
        !galleryUploadComplete || expected != galleryHash) {
      Serial.println(F("ERR gallery checksum"));
      return;
    }
    String destination = galleryPath(galleryUploadSlot);
    if (!LittleFS.rename(GALLERY_UPLOAD_PATH, destination)) {
      galleryUploadComplete = false;
      Serial.println(F("ERR gallery storage unavailable"));
      return;
    }
    galleryUploadComplete = false;
    gallerySlot = galleryUploadSlot;
    cancelAlert();
    setDisplayPersistence(ScreenMode::GALLERY);
    screenMode = ScreenMode::GALLERY;
    galleryPlaying = false;
    saveGalleryPlayback();
    screenDirty = true;
    Serial.println(F("OK gallery uploaded"));
  } else if (!strcasecmp(action, "SHOW")) {
    if (!first || second || !parseGallerySlot(first, slot) || !galleryExists(slot)) {
      Serial.println(F("ERR gallery slot must be 0-6 and contain an image"));
      return;
    }
    gallerySlot = slot;
    cancelAlert();
    setDisplayPersistence(ScreenMode::GALLERY);
    screenMode = ScreenMode::GALLERY;
    galleryPlaying = false;
    saveGalleryPlayback();
    screenDirty = true;
    Serial.println(F("OK gallery shown"));
  } else if (!strcasecmp(action, "DELETE")) {
    if (!first || second || !parseGallerySlot(first, slot) ||
        !LittleFS.remove(galleryPath(slot))) {
      Serial.println(F("ERR gallery delete failed"));
      return;
    }
    if (slot == gallerySlot) {
      if (!galleryPlaying || !advanceGallery()) galleryPlaying = false;
      saveGalleryPlayback();
      if (screenMode == ScreenMode::GALLERY) screenDirty = true;
    }
    Serial.println(F("OK gallery deleted"));
  } else if (!strcasecmp(action, "PLAY")) {
    char* end = nullptr;
    long seconds = first ? strtol(first, &end, 10) : -1;
    if (!first || second || !end || *end || seconds < 3 || seconds > 300 ||
        !galleryExists(gallerySlot)) {
      Serial.println(F("ERR gallery play needs 3-300 seconds and an image"));
      return;
    }
    galleryIntervalMillis = static_cast<uint32_t>(seconds) * 1000UL;
    cancelAlert();
    setDisplayPersistence(ScreenMode::GALLERY);
    galleryPlaying = true;
    lastGalleryAdvance = millis();
    screenMode = ScreenMode::GALLERY;
    saveGalleryPlayback();
    screenDirty = true;
    Serial.println(F("OK gallery slideshow started"));
  } else if (!strcasecmp(action, "STOP")) {
    if (first || second) {
      Serial.println(F("ERR gallery stop takes no arguments"));
      return;
    }
    galleryPlaying = false;
    saveGalleryPlayback();
    Serial.println(F("OK gallery slideshow stopped"));
  } else {
    Serial.println(F("ERR gallery action"));
  }
}

void handleSet(char* input) {
  char* key = strtok(input, " ");
  char* value = strtok(nullptr, "");
  if (!key || !value) {
    Serial.println(F("ERR usage: SET <key> <value>"));
    return;
  }
  while (*value == ' ') ++value;

  if (!strcasecmp(key, "TITLE")) {
    copyText(cfg.title, sizeof(cfg.title), value);
  } else if (!strcasecmp(key, "CITY")) {
    copyText(cfg.city, sizeof(cfg.city), value);
  } else if (!strcasecmp(key, "BRIGHTNESS")) {
    char* end = nullptr;
    long brightness = strtol(value, &end, 10);
    if (!end || *end || brightness < 0 || brightness > 100) {
      Serial.println(F("ERR brightness must be 0-100"));
      return;
    }
    cfg.brightness = static_cast<uint8_t>(brightness);
    applyBrightness();
  } else if (!strcasecmp(key, "ROTATION")) {
    char* end = nullptr;
    long rotation = strtol(value, &end, 10);
    if (!end || *end || rotation < 0 || rotation > 3) {
      Serial.println(F("ERR rotation must be 0-3"));
      return;
    }
    cfg.rotation = static_cast<uint8_t>(rotation);
    display->setRotation(cfg.rotation);
  } else if (!strcasecmp(key, "BLINVERT")) {
    bool inverted = false;
    if (!parseBool(value, inverted)) {
      Serial.println(F("ERR blinvert must be on or off"));
      return;
    }
    cfg.backlightInverted = inverted;
    applyBrightness();
  } else if (!strcasecmp(key, "ACCENT")) {
    if (!parseHexColor(value, cfg.accent)) {
      Serial.println(F("ERR accent must be #RRGGBB"));
      return;
    }
  } else if (!strcasecmp(key, "TIME")) {
    int hour = -1;
    int minute = -1;
    char extra = 0;
    if (sscanf(value, "%d:%d%c", &hour, &minute, &extra) != 2 ||
        hour < 0 || hour > 23 || minute < 0 || minute > 59) {
      Serial.println(F("ERR time must be HH:MM"));
      return;
    }
    baseMinuteOfDay = hour * 60 + minute;
    baseTimeMillis = millis();
    timeWasSet = true;
    lastReminderCheckMinute = EMPTY_REMINDER;
    screenDirty = screenMode == ScreenMode::CLOCK;
    if (screenMode == ScreenMode::FACE && faceMood == FaceMood::AUTO) {
      faceDirty = true;
    }
    Serial.println(F("OK"));
    return;
  } else {
    Serial.println(F("ERR unknown setting"));
    return;
  }

  screenDirty = true;
  Serial.println(F("OK"));
}

void handleCommand(char* line) {
  while (*line == ' ') ++line;
  size_t len = strlen(line);
  while (len && line[len - 1] == ' ') line[--len] = 0;
  if (!len) return;

  if (!strcasecmp(line, "HELP")) {
    printHelp();
  } else if (!strcasecmp(line, "STATUS")) {
    printStatus();
  } else if (!strncasecmp(line, "SET ", 4)) {
    handleSet(line + 4);
  } else if (!strncasecmp(line, "USAGE ", 6)) {
    if (parseUsage(line + 6)) {
      Serial.println(F("OK usage updated"));
    } else {
      Serial.println(F("ERR usage: USAGE requires six percentages (0-100)"));
    }
  } else if (!strncasecmp(line, "RESETS ", 7)) {
    if (parseResets(line + 7)) {
      Serial.println(F("OK resets updated"));
    } else {
      Serial.println(F("ERR resets: RESETS requires six minute values (-1 to 65535)"));
    }
  } else if (!strncasecmp(line, "BTC ", 4)) {
    if (parseBtc(line + 4)) {
      Serial.println(F("OK btc updated"));
    } else {
      Serial.println(F("ERR btc snapshot invalid"));
    }
  } else if (!strncasecmp(line, "ACTIVE ", 7)) {
    if (parseActive(line + 7)) {
      usageDirty = true;
      Serial.println(F("OK active account updated"));
    } else {
      Serial.println(F("ERR active: use C1-C3, X1-X3, or OFF"));
    }
  } else if (!strncasecmp(line, "SCREEN ", 7)) {
    if (parseScreen(line + 7)) {
      screenDirty = true;
      Serial.println(F("OK screen changed"));
    } else {
      Serial.println(F("ERR screen: use FACE, USAGE, BTC, CLOCK, or THAI"));
    }
  } else if (!strncasecmp(line, "FACE ", 5)) {
    if (setFace(line + 5)) {
      screenDirty = true;
      Serial.println(F("OK face changed"));
    } else {
      Serial.println(F("ERR face: use AUTO, NEUTRAL, HAPPY, FOCUS, CURIOUS, SLEEPY, ALERT, or CELEBRATE"));
    }
  } else if (!strncasecmp(line, "EMOTION ", 8)) {
    if (setAutoEmotion(line + 8)) {
      Serial.println(F("OK emotion updated"));
    } else {
      Serial.println(F("ERR emotion: use NEUTRAL, HAPPY, FOCUS, CURIOUS, SLEEPY, ALERT, CELEBRATE, or CLEAR"));
    }
  } else if (!strncasecmp(line, "GALLERY ", 8)) {
    handleGallery(line + 8);
  } else if (!strncasecmp(line, "REMIND ", 7)) {
    handleRemind(line + 7);
  } else if (!strcasecmp(line, "SHOW")) {
    screenDirty = true;
    Serial.println(F("OK"));
  } else if (!strcasecmp(line, "TEST")) {
    cancelAlert();
    drawTestPattern();
    Serial.println(F("OK test pattern"));
  } else if (!strcasecmp(line, "SAVE")) {
    Serial.println(saveConfig() ? F("OK saved") : F("ERR save failed"));
  } else if (!strcasecmp(line, "RESETCFG")) {
    setDefaults();
    faceMood = persistedFaceMood();
    autoFaceMoodSet = false;
    applyBrightness();
    display->setRotation(cfg.rotation);
    screenDirty = true;
    Serial.println(F("OK defaults loaded; use SAVE to persist"));
  } else if (!strcasecmp(line, "REBOOT")) {
    Serial.println(F("OK rebooting"));
    Serial.flush();
    delay(50);
    ESP.restart();
  } else {
    Serial.println(F("ERR unknown command; type HELP"));
  }
}

void serviceSerial() {
  if (galleryRemaining) {
    while (Serial.available() && galleryRemaining) {
      uint8_t value = static_cast<uint8_t>(Serial.read());
      if (galleryUpload.write(value) != 1) {
        galleryUpload.close();
        galleryRemaining = 0;
        galleryUploadComplete = false;
        LittleFS.remove(GALLERY_UPLOAD_PATH);
        Serial.println(F("ERR gallery write failed"));
        return;
      }
      galleryHash ^= value;
      galleryHash *= 16777619UL;
      --galleryRemaining;
    }
    if (!galleryRemaining) {
      galleryUpload.close();
      galleryUploadComplete = true;
      Serial.println(F("OK gallery data received"));
    }
    return;
  }
  while (Serial.available()) {
    char c = static_cast<char>(Serial.read());
    if (c == '\r') continue;
    if (c == '\n') {
      if (serialDiscardingLine) {
        serialDiscardingLine = false;
        serialLength = 0;
        continue;
      }
      serialLine[serialLength] = 0;
      handleCommand(serialLine);
      serialLength = 0;
      continue;
    }
    if (serialLength < sizeof(serialLine) - 1) {
      serialLine[serialLength++] = c;
    } else {
      serialLength = 0;
      serialDiscardingLine = true;
      Serial.println(F("ERR line too long"));
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(50);
  delay(50);
  Serial.println();
  Serial.println(F("USB_CLOCK 0.1 READY"));

  bool loaded = loadConfig();
  faceMood = persistedFaceMood();
  uint8_t persistedScreen = cfg.displayState & DISPLAY_SCREEN_MASK;
  if (persistedScreen == DISPLAY_SCREEN_BTC) screenMode = ScreenMode::BTC;
  else if (persistedScreen == DISPLAY_SCREEN_FACE) screenMode = ScreenMode::FACE;
  loadGalleryPlayback();
  loadReminders();
  Serial.println(loaded ? F("CONFIG loaded") : F("CONFIG defaults"));

  pinMode(TFT_BL, OUTPUT);
  analogWriteRange(255);
  applyBrightness();

  bus = new Arduino_HWSPI(TFT_DC, TFT_CS);
  display = new ClockST7789(bus, TFT_RST, 0, true,
                            SCREEN_W, SCREEN_H, 0, 0, 0, 0);
  display->begin();
  display->setRotation(cfg.rotation);
  display->setTextWrap(false);
  randomSeed(micros() ^ ESP.getChipId());
  if (!LittleFS.begin()) {
    LittleFS.format();
    LittleFS.begin();
  }
  drawScreen();
  printStatus();
}

void loop() {
  serviceSerial();
  checkReminders();
  if (alertActive && millis() - alertStartMillis >= ALERT_DURATION_MILLIS) {
    alertActive = false;
    screenMode = alertReturnMode;
    lastGalleryAdvance = millis();
    // Re-check the same catch-up window so reminders sharing a minute are
    // displayed sequentially instead of being marked fired while hidden.
    lastReminderCheckMinute = EMPTY_REMINDER;
    screenDirty = true;
  }
  if (alertActive) {
    // The alert is drawn once on entry; do not redraw it every loop tick.
  } else if (screenDirty) {
    drawScreen();
  } else if (cryptoDirty && !testPatternActive && screenMode == ScreenMode::BTC) {
    drawCryptoRegion();
  } else if (usageDirty && !testPatternActive && screenMode == ScreenMode::USAGE) {
    drawUsageRegion();
    usageDirty = false;
  } else if (!testPatternActive && screenMode == ScreenMode::USAGE && usage.valid &&
             millis() / 60000UL != lastSyncMinuteDrawn) {
    drawUsageSummary();
  } else if (!testPatternActive && screenMode == ScreenMode::CLOCK &&
             millis() / 60000UL != lastSyncMinuteDrawn) {
    drawClockTime();
    lastSyncMinuteDrawn = millis() / 60000UL;
  } else if (!testPatternActive && screenMode == ScreenMode::FACE) {
    serviceFaceAnimation();
  } else if (!testPatternActive && screenMode == ScreenMode::GALLERY && galleryPlaying &&
             millis() - lastGalleryAdvance >= galleryIntervalMillis) {
    lastGalleryAdvance = millis();
    if (advanceGallery()) screenDirty = true;
  }
  delay(2);
}
