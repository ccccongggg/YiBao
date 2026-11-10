/*
 * Step1 硬件自检固件 —— ESP32-S3-N16R8
 *
 * 验证内容（每 250ms 打印一行 DATA）:
 *   1. 摇杆:   X=GPIO1(ADC)  Y=GPIO2(ADC)  SW=GPIO3(低电平有效)
 *   2. 按键:   GPIO7(低电平有效, 内部上拉)
 *   3. 压力:   GPIO6(ADC, HW-103 AO)
 *   4. ICM-20602 SPI: SCK=10 MOSI=9 MISO=12 CS=11, WHO_AM_I(0x75) 应为 0x12
 *   5. 舵机:   1/2/3 = GPIO4/5/18, 50Hz, 全部输出停止脉宽 1500us(360°舵机应静止)
 *
 * 串口 115200
 */
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_idf_version.h"
#include "soc/gpio_num.h"

/* ================= 引脚定义（严格按接线规划） ================= */
#define PIN_JOY_X    GPIO_NUM_1    /* VRx 模拟 */
#define PIN_JOY_Y    GPIO_NUM_2    /* VRy 模拟 */
#define PIN_JOY_SW   GPIO_NUM_3    /* 摇杆按压, 低电平有效 */
#define PIN_SRV1     GPIO_NUM_4    /* 舵机1 PWM */
#define PIN_SRV2     GPIO_NUM_5    /* 舵机2 PWM */
#define PIN_PRESS    GPIO_NUM_6    /* HW-103 AO 模拟 */
#define PIN_BTN      GPIO_NUM_7    /* 模式按键, 低电平有效 */
#define PIN_MOSI     GPIO_NUM_9    /* ICM SDA/SDI */
#define PIN_MISO     GPIO_NUM_12   /* ICM SAO/SDO */
#define PIN_SCLK     GPIO_NUM_10   /* ICM SCL/SPC */
#define PIN_CS       GPIO_NUM_11   /* ICM CS */
#define PIN_SRV3     GPIO_NUM_18   /* 舵机3 PWM */

#define SERVO_FREQ_HZ      50
#define SERVO_STOP_US      1500   /* 360°舵机停止脉宽 */
#define LEDC_RES           LEDC_TIMER_14_BIT
#define LEDC_MAX_DUTY      (1 << 14)

static const char *WHO_EXPECT = "0x12";
#define ICM_WHO_AM_I_REG  0x75
#define ICM_PWR_MGMT_1    0x6B

static adc_oneshot_unit_handle_t s_adc1;
static spi_device_handle_t s_icm;

/* ---------------- 舵机 ---------------- */
static void servo_stop_all(void)
{
    /* 停止脉宽: 1500us -> duty = us*16384/20000 */
    int duty = (int)((long long)SERVO_STOP_US * LEDC_MAX_DUTY / (1000000 / SERVO_FREQ_HZ));
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1);
    ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_2, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_2);
}

static void servo_init(void)
{
    ledc_timer_config_t t = {
        .speed_mode      = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_RES,
        .timer_num       = LEDC_TIMER_0,
        .freq_hz         = SERVO_FREQ_HZ,
        .clk_cfg         = LEDC_AUTO_CLK,
    };
    ledc_timer_config(&t);

    const gpio_num_t pins[3] = {PIN_SRV1, PIN_SRV2, PIN_SRV3};
    for (int i = 0; i < 3; i++) {
        ledc_channel_config_t c = {
            .gpio_num   = pins[i],
            .speed_mode = LEDC_LOW_SPEED_MODE,
            .channel    = (ledc_channel_t)i,
            .intr_type  = LEDC_INTR_DISABLE,
            .timer_sel  = LEDC_TIMER_0,
            .duty       = 0,
            .hpoint     = 0,
        };
        ledc_channel_config(&c);
    }
    servo_stop_all();
}

/* ---------------- ICM-20602 SPI ---------------- */
static void icm_cs_set(int level)
{
    gpio_set_level(PIN_CS, level);
}

static esp_err_t icm_read(uint8_t reg, uint8_t *out, size_t len)
{
    uint8_t tx[16] = {0};
    uint8_t rx[16] = {0};
    tx[0] = reg | 0x80;               /* MSB=1 表示读 */
    spi_transaction_t t = {0};
    t.length    = (len + 1) * 8;
    t.rxlength  = (len + 1) * 8;
    t.tx_buffer = tx;
    t.rx_buffer = rx;
    icm_cs_set(0);
    esp_err_t err = spi_device_transmit(s_icm, &t);
    icm_cs_set(1);
    if (err == ESP_OK) memcpy(out, &rx[1], len);
    return err;
}

static esp_err_t icm_write(uint8_t reg, uint8_t val)
{
    uint8_t tx[2] = { (uint8_t)(reg & 0x7F), val };
    spi_transaction_t t = {0};
    t.length    = 16;
    t.tx_buffer = tx;
    icm_cs_set(0);
    esp_err_t err = spi_device_transmit(s_icm, &t);
    icm_cs_set(1);
    return err;
}

static void icm_init(void)
{
    /* CS 引脚: 手动控制, 上拉, 平时为高 */
    gpio_config_t cs = {
        .pin_bit_mask = 1ULL << PIN_CS,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&cs);
    icm_cs_set(1);

    spi_bus_config_t bus = {
        .mosi_io_num     = PIN_MOSI,
        .miso_io_num     = PIN_MISO,
        .sclk_io_num     = PIN_SCLK,
        .quadwp_io_num   = -1,
        .quadhd_io_num   = -1,
    };
    spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO);

    spi_device_interface_config_t dev = {
        .clock_speed_hz = 1 * 1000 * 1000,   /* 1MHz, 自检阶段保守 */
        .mode           = 0,                 /* ICM-20602: SPI mode 0/3 */
        .spics_io_num   = -1,                /* CS 手动控制 */
        .queue_size     = 1,
    };
    spi_bus_add_device(SPI2_HOST, &dev, &s_icm);
}

/* ---------------- 外设初始化 ---------------- */
static void peripherals_init(void)
{
    /* ADC1: GPIO1->CH0, GPIO2->CH1, GPIO6->CH5 */
    adc_oneshot_unit_init_cfg_t init = { .unit_id = ADC_UNIT_1 };
    adc_oneshot_new_unit(&init, &s_adc1);
    adc_oneshot_chan_cfg_t ch = {
        .atten    = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,   /* 12bit: 0~4095 */
    };
    adc_oneshot_config_channel(s_adc1, ADC_CHANNEL_0, &ch);
    adc_oneshot_config_channel(s_adc1, ADC_CHANNEL_1, &ch);
    adc_oneshot_config_channel(s_adc1, ADC_CHANNEL_5, &ch);

    /* 数字输入: 摇杆SW + 模式按键, 均低电平有效, 内部上拉 */
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << PIN_JOY_SW) | (1ULL << PIN_BTN),
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
}

void app_main(void)
{
    printf("\n\n============================================\n");
    printf("  ESP32-S3 Step1 硬件自检固件\n");
    printf("============================================\n");

    /* 芯片信息 */
    esp_chip_info_t ci;
    esp_chip_info(&ci);
    uint32_t flash_mb = 0;
    esp_flash_get_size(NULL, &flash_mb);
    flash_mb /= (1024 * 1024);
    size_t psram_kb = heap_caps_get_total_size(MALLOC_CAP_SPIRAM) / 1024;
    printf("[芯片] %s 核心%d 版本%d.%d | Flash: %uMB | PSRAM: %uKB | IDF: %s\n",
           (ci.model == CHIP_ESP32S3) ? "ESP32-S3" : "OTHER",
           ci.cores, ci.revision / 100, ci.revision % 100,
           (unsigned)flash_mb, (unsigned)psram_kb, esp_get_idf_version());

    peripherals_init();
    icm_init();
    servo_init();

    /* ICM WHO_AM_I 自检 */
    uint8_t who = 0;
    esp_err_t err = icm_read(ICM_WHO_AM_I_REG, &who, 1);
    if (err != ESP_OK) {
        printf("[ICM] SPI 读取失败: %s (检查接线 SCK=10/MOSI=9/MISO=12/CS=11)\n", esp_err_to_name(err));
    } else {
        printf("[ICM] WHO_AM_I = 0x%02X (期望 %s) %s\n", who, WHO_EXPECT,
               who == 0x12 ? "<<< 正确" : "<<< 不匹配! 检查供电/接线/SPI模式");
        if (who == 0x12) {
            /* 唤醒芯片并再读一次确认 */
            icm_write(ICM_PWR_MGMT_1, 0x00);
            vTaskDelay(pdMS_TO_TICKS(50));
            uint8_t who2 = 0;
            icm_read(ICM_WHO_AM_I_REG, &who2, 1);
            printf("[ICM] 唤醒后 WHO_AM_I = 0x%02X\n", who2);
        }
    }

    printf("[舵机] 三路舵机已输出停止脉宽 1500us (50Hz) —— 现在应保持静止不动\n");
    printf("\n>>> 请配合进行以下操作并观察 DATA 行:\n");
    printf(">>>  1. 推摇杆X/Y  -> joyX/joyY 应在 0~4095 之间变化, 松开回中位(约1800)\n");
    printf(">>>  2. 按下按键   -> btn=0, 松开 btn=1\n");
    printf(">>>  3. 按压压力膜 -> press 数值应明显上升, 松开回落\n");
    printf(">>>  4. 按下摇杆   -> joySW=0\n");
    printf(">>>  5. 观察舵机  -> 三个舵机应全程静止\n");
    printf(">>>  6. 观察 whoami -> 应为 0x12\n");
    printf("--------------------------------------------\n\n");

    int cnt = 0;
    while (1) {
        int jx = 0, jy = 0, press = 0;
        adc_oneshot_read(s_adc1, ADC_CHANNEL_0, &jx);
        adc_oneshot_read(s_adc1, ADC_CHANNEL_1, &jy);
        adc_oneshot_read(s_adc1, ADC_CHANNEL_5, &press);
        int btn    = gpio_get_level(PIN_BTN);    /* 0=按下 */
        int joy_sw = gpio_get_level(PIN_JOY_SW); /* 0=按下 */

        printf("DATA n=%d btn=%d joySW=%d joyX=%4d joyY=%4d press=%4d whoami=0x%02X\n",
               cnt++, btn, joy_sw, jx, jy, press, who);

        vTaskDelay(pdMS_TO_TICKS(250));
    }
}
