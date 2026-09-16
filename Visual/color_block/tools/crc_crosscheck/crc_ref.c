/*
 * crc_ref.c —— CRC-8 交叉验证参考实现
 * ============================================================
 * 本文件中的 bsp_game_crc8() 是从 STM32 固件原样摘录的：
 *     SmartClock/Code/BSP/Src/bsp_board.c
 * 未做任何修改（仅去掉了 static，以便跨文件引用）。
 *
 * 用途：与视觉端 Python 实现 src/protocol.py::crc8 做逐比特交叉比对，
 *       确保两边算法完全一致。这是"真的对齐了"而不是"看起来一样"。
 *
 * 用法：
 *     ./crc_ref            # 从 stdin 逐行读十六进制字节串，逐行输出 CRC
 *     echo "06 01 D4 00 42 02 00" | ./crc_ref
 *
 * 编译：
 *     gcc -O2 -Wall -Wextra -o crc_ref crc_ref.c
 */

#include <stdio.h>
#include <stdint.h>
#include <string.h>

/* ── 以下函数体与固件 bsp_board.c:208 完全一致（仅去掉 static） ───────── */
uint8_t bsp_game_crc8(const uint8_t *data, uint8_t length)
{
  uint8_t crc = 0U;

  for (uint8_t i = 0U; i < length; i++)
  {
    crc ^= data[i];
    for (uint8_t bit = 0U; bit < 8U; bit++)
      crc = (crc & 0x80U) ? (uint8_t)((crc << 1U) ^ 0x07U) : (uint8_t)(crc << 1U);
  }
  return crc;
}
/* ── 摘录结束 ─────────────────────────────────────────────────────────── */

#define MAX_BYTES 64

int main(void)
{
  char line[1024];

  while (fgets(line, sizeof(line), stdin) != NULL)
  {
    uint8_t data[MAX_BYTES];
    uint8_t length = 0U;
    char *p = line;

    /* 逐个人工解析十六进制字节，避免依赖 strtok 的全局状态 */
    while (*p != '\0' && length < MAX_BYTES)
    {
      while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
      if (*p == '\0') break;

      unsigned int value = 0U;
      if (sscanf(p, "%2x", &value) != 1) break;
      data[length++] = (uint8_t)value;

      /* 跳过刚读的两个十六进制字符 */
      while (*p != '\0' && *p != ' ' && *p != '\t' &&
             *p != '\n' && *p != '\r') p++;
    }

    if (length == 0U) continue;
    printf("%u\n", (unsigned int)bsp_game_crc8(data, length));
  }
  return 0;
}
