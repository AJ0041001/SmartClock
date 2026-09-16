#ifndef __BSP_BOARD_H
#define __BSP_BOARD_H

#include <stdint.h>

void BSP_Board_Init(void);
void BSP_Log(const char *text);
void BSP_LED_Set(uint8_t led, uint8_t on);
void BSP_Buzzer_Set(uint8_t on);
void BSP_Buzzer_Beep(uint32_t milliseconds);
uint8_t BSP_Key_Read(void);
uint16_t BSP_ADC_Average(void);

#define BSP_ADC_SCOPE_POINTS 96U

typedef enum
{
  BSP_ADC_WAVE_UNKNOWN = 0U,
  BSP_ADC_WAVE_SINE,
  BSP_ADC_WAVE_SQUARE,
  BSP_ADC_WAVE_TRIANGLE,
  BSP_ADC_WAVE_SAWTOOTH
} BspAdcWaveform;

typedef struct
{
  uint16_t samples[BSP_ADC_SCOPE_POINTS];
  uint16_t minimum;
  uint16_t maximum;
  uint16_t dc_mv;
  uint16_t vpp_mv;
  uint32_t frequency_x10;
  uint16_t duty_x10;
  BspAdcWaveform waveform;
  uint8_t valid;
} BspAdcScopeFrame;

uint8_t BSP_ADC_GetScopeFrame(BspAdcScopeFrame *frame);

typedef enum
{
  BSP_DAC_WAVE_SINE = 0U,
  BSP_DAC_WAVE_SQUARE,
  BSP_DAC_WAVE_TRIANGLE,
  BSP_DAC_WAVE_SAWTOOTH
} BspDacWaveform;

typedef struct
{
  BspDacWaveform waveform;
  uint16_t vpp_x10;       /* Peak-to-peak voltage in 0.1 V, 0..33 (0..3.3 V). */
  uint8_t duty_percent;   /* 0..100 %, used by the square wave. */
  uint16_t frequency_x10; /* Output frequency in 0.1 Hz, 0..20000 (0..2000 Hz). */
  uint8_t running;        /* 0 = paused, non-zero = output enabled. */
} BspDacConfig;

void BSP_DAC_StartTestWave(void);
uint8_t BSP_DAC_ApplyConfig(const BspDacConfig *config);
void BSP_DAC_Stop(void);
uint8_t BSP_Audio_InitAndStartTone(void);
void BSP_Audio_Stop(void);
uint8_t BSP_Audio_PlayHour(uint8_t hour);
void BSP_Audio_RequestHour(uint8_t hour);
uint8_t BSP_Audio_PlayTestVoice(void);
void BSP_Audio_SetVoicePack(uint8_t pack);
void BSP_Audio_SetGameMusicPack(uint8_t pack);
void BSP_Audio_SetVolume(uint8_t percent);
uint8_t BSP_Audio_GetVolume(void);

typedef enum
{
  BSP_AUDIO_EFFECT_STAR1 = 0U,
  BSP_AUDIO_EFFECT_STAR2,
  BSP_AUDIO_EFFECT_GAMEOVER,
  BSP_AUDIO_EFFECT_BOMB
} BspAudioEffect;

uint8_t BSP_Audio_PlayEffect(BspAudioEffect effect);
void BSP_Audio_RequestEffect(BspAudioEffect effect);
void BSP_Audio_ProcessPending(void);
uint8_t BSP_Audio_GetLastFatFsResult(void);
uint8_t BSP_Audio_GetLastStatus(void);
uint8_t BSP_Audio_GetLastHour(void);
void BSP_TimeChime_SetEnabled(uint8_t enabled);
uint8_t BSP_TimeChime_IsEnabled(void);
void BSP_TimeChime_SetTimeEditing(uint8_t editing);
uint8_t BSP_TimeChime_IsTimeEditing(void);
void BSP_TimeChime_RequestAfterTimeEdit(void);
uint8_t BSP_TimeChime_IsAfterTimeEditPending(void);
void BSP_TimeChime_ClearAfterTimeEdit(void);
uint8_t BSP_LCD_InitAndShow(void);
uint8_t BSP_SD_MountAndProbe(void);

/* Game remote-control input received from USART2.  TYPE 01 means that the
   latest X coordinate is combined with keypad movement; TYPE 02 means that
   the serial coordinates are ignored and the keypad has exclusive control. */
typedef struct
{
  uint8_t mode;
  uint16_t x;
  uint16_t y;
  uint32_t generation;
  uint32_t last_tick;
} BspGameSerialControl;

void BSP_GameSerial_Start(void);
uint8_t BSP_GameSerial_GetLatest(BspGameSerialControl *control);
void BSP_GameSerial_Service(void);

/* The two user alarms are backed by the STM32 RTC Alarm A and Alarm B
   hardware.  `enabled` controls the normal once-per-day schedule. */
typedef struct
{
  uint8_t hours;
  uint8_t minutes;
  uint8_t seconds;
  uint8_t enabled;
} BspAlarmConfig;

#define BSP_ALARM_A 0U
#define BSP_ALARM_B 1U

void BSP_Alarm_Init(void);
void BSP_Alarm_Get(uint8_t alarm_id, BspAlarmConfig *config);
uint8_t BSP_Alarm_Set(uint8_t alarm_id, const BspAlarmConfig *config);
uint8_t BSP_Alarm_TakePending(uint8_t *alarm_id);
void BSP_Alarm_Snooze(uint8_t alarm_id);
void BSP_Alarm_Dismiss(uint8_t alarm_id);
void BSP_Alarm_Timeout(uint8_t alarm_id);

#endif
