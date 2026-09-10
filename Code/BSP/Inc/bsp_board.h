#ifndef __BSP_BOARD_H
#define __BSP_BOARD_H

#include <stdint.h>

void BSP_Board_Init(void);
void BSP_Log(const char *text);
void BSP_LED_Set(uint8_t led, uint8_t on);
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
uint8_t BSP_LCD_InitAndShow(void);
uint8_t BSP_SD_MountAndProbe(void);

#endif
