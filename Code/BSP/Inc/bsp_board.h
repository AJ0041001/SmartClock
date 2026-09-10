#ifndef __BSP_BOARD_H
#define __BSP_BOARD_H

#include <stdint.h>

void BSP_Board_Init(void);
void BSP_Log(const char *text);
void BSP_LED_Set(uint8_t led, uint8_t on);
void BSP_Buzzer_Beep(uint32_t milliseconds);
uint8_t BSP_Key_Read(void);
uint16_t BSP_ADC_Average(void);
void BSP_DAC_StartTestWave(void);
uint8_t BSP_Audio_InitAndStartTone(void);
void BSP_Audio_Stop(void);
uint8_t BSP_LCD_InitAndShow(void);
uint8_t BSP_SD_MountAndProbe(void);

#endif
