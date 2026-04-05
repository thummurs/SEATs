// ============================================
// SEATs — ESP32-S3-EYE Face Capture
// Polls Flask API for pending verifications,
// captures face and sends to FastAPI/Rekognition
// ============================================
// Built with ESP-IDF v6.1
// esp32-camera component required
// ============================================

#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "nvs_flash.h"
#include "esp_http_client.h"
#include "esp_camera.h"
#include "esp_crt_bundle.h"

static const char* TAG = "SEATs-EYE";

// --- Config ---
#define WIFI_SSID        "Cumberland"
#define WIFI_PASS        "Cumberland7"
#define FLASK_API_URL    "https://seats-production-4c03.up.railway.app"
#define FASTAPI_URL      "https://seats-face-api-production.up.railway.app"
#define API_KEY          "c32d2eb7db57fb3cf743ca72c53cd8971579cbba99c53e77f80ea282405170d3"

// How often to poll Flask for pending verifications (ms)
#define POLL_INTERVAL_MS  2000

// --- Camera pins for ESP32-S3-EYE ---
#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK    15
#define CAM_PIN_SIOD    4
#define CAM_PIN_SIOC    5
#define CAM_PIN_D7      16
#define CAM_PIN_D6      17
#define CAM_PIN_D5      18
#define CAM_PIN_D4      12
#define CAM_PIN_D3      10
#define CAM_PIN_D2      8
#define CAM_PIN_D1      9
#define CAM_PIN_D0      11
#define CAM_PIN_VSYNC   6
#define CAM_PIN_HREF    7
#define CAM_PIN_PCLK    13

static bool wifi_connected = false;


// ============================================
// WiFi
// ============================================
static void wifi_event_handler(void* arg, esp_event_base_t event_base,
                                int32_t event_id, void* event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_connected = false;
        ESP_LOGW(TAG, "WiFi disconnected, reconnecting...");
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        ESP_LOGI(TAG, "WiFi connected. IP: " IPSTR, IP2STR(&event->ip_info.ip));
        wifi_connected = true;
    }
}

static void init_wifi(void) {
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                &wifi_event_handler, NULL));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid     = WIFI_SSID,
            .password = WIFI_PASS,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_connect());

    // Wait for connection (max 10s)
    for (int i = 0; i < 20 && !wifi_connected; i++) {
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}


// ============================================
// Camera
// ============================================
static void init_camera(void) {
    camera_config_t config = {
        .pin_pwdn     = CAM_PIN_PWDN,
        .pin_reset    = CAM_PIN_RESET,
        .pin_xclk     = CAM_PIN_XCLK,
        .pin_sscb_sda = CAM_PIN_SIOD,
        .pin_sscb_scl = CAM_PIN_SIOC,
        .pin_d7       = CAM_PIN_D7,
        .pin_d6       = CAM_PIN_D6,
        .pin_d5       = CAM_PIN_D5,
        .pin_d4       = CAM_PIN_D4,
        .pin_d3       = CAM_PIN_D3,
        .pin_d2       = CAM_PIN_D2,
        .pin_d1       = CAM_PIN_D1,
        .pin_d0       = CAM_PIN_D0,
        .pin_vsync    = CAM_PIN_VSYNC,
        .pin_href     = CAM_PIN_HREF,
        .pin_pclk     = CAM_PIN_PCLK,
        .xclk_freq_hz = 20000000,
        .ledc_timer   = LEDC_TIMER_0,
        .ledc_channel = LEDC_CHANNEL_0,
        .pixel_format = PIXFORMAT_JPEG,
        .frame_size   = FRAMESIZE_QVGA,
        .jpeg_quality = 12,
        .fb_count     = 1,
        .fb_location  = CAMERA_FB_IN_DRAM,
        .grab_mode    = CAMERA_GRAB_LATEST,
    };

    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Camera init failed: 0x%x", err);
        return;
    }
    ESP_LOGI(TAG, "Camera initialized");
}


// ============================================
// HTTP helpers
// ============================================

static char* http_get(const char* url) {
    static char response_buf[2048];
    memset(response_buf, 0, sizeof(response_buf));

    esp_http_client_config_t config = {
        .url                         = url,
        .timeout_ms                  = 5000,
        .skip_cert_common_name_check = true,
        .crt_bundle_attach           = esp_crt_bundle_attach,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    esp_http_client_set_header(client, "X-API-Key", API_KEY);

    esp_err_t err = esp_http_client_open(client, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "GET failed to open: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return response_buf;
    }

    int content_length = esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);
    ESP_LOGI(TAG, "GET %s → %d (%d bytes)", url, status, content_length);

    int data_read = esp_http_client_read_response(client, response_buf, sizeof(response_buf) - 1);
    if (data_read >= 0) {
        response_buf[data_read] = '\0';
    }
    ESP_LOGI(TAG, "Response body: %s", response_buf);

    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return response_buf;
}

// POST raw bytes — for sending JPEG to FastAPI
static int http_post_jpeg(const char* url, const char* verification_id,
                           uint8_t* data, size_t len) {
    esp_http_client_config_t config = {
        .url                        = url,
        .method                     = HTTP_METHOD_POST,
        .timeout_ms                 = 10000,
        .skip_cert_common_name_check = true,
        .use_global_ca_store        = false,
        .crt_bundle_attach          = esp_crt_bundle_attach,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    esp_http_client_set_header(client, "Content-Type",       "image/jpeg");
    esp_http_client_set_header(client, "X-API-Key",          API_KEY);
    esp_http_client_set_header(client, "X-Verification-Id",  verification_id);
    esp_http_client_set_post_field(client, (const char*)data, len);

    int status = -1;
    esp_err_t err = esp_http_client_perform(client);
    if (err == ESP_OK) {
        status = esp_http_client_get_status_code(client);
        ESP_LOGI(TAG, "POST JPEG → %d", status);
    } else {
        ESP_LOGE(TAG, "POST JPEG failed: %s", esp_err_to_name(err));
    }
    esp_http_client_cleanup(client);
    return status;
}


// ============================================
// Capture + send
// ============================================
static void capture_and_send(const char* verification_id, const char* student_name) {
    ESP_LOGI(TAG, "Capturing for: %s (verification %s)", student_name, verification_id);

    // Discard first frame (warm-up)
    camera_fb_t* fb = esp_camera_fb_get();
    if (fb) esp_camera_fb_return(fb);
    vTaskDelay(pdMS_TO_TICKS(100));

    // Capture real frame
    fb = esp_camera_fb_get();
    if (!fb) {
        ESP_LOGE(TAG, "Camera capture failed");
        return;
    }

    ESP_LOGI(TAG, "Captured %zu bytes", fb->len);

    // Send to FastAPI
    char url[128];
    snprintf(url, sizeof(url), "%s/recognize", FASTAPI_URL);
    int status = http_post_jpeg(url, verification_id, fb->buf, fb->len);

    esp_camera_fb_return(fb);

    if (status == 200) {
        ESP_LOGI(TAG, "Face sent successfully");
    } else {
        ESP_LOGW(TAG, "FastAPI returned %d", status);
    }
}


// ============================================
// Main polling task
// ============================================
static void polling_task(void* pvParameters) {
    ESP_LOGI(TAG, "Polling Flask for pending verifications every %dms", POLL_INTERVAL_MS);

    char url[256];
    snprintf(url, sizeof(url), "%s/api/face/pending", FLASK_API_URL);

    while (1) {
        if (!wifi_connected) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        char* body = http_get(url);
        if (body && strlen(body) > 0) {
            char* pending_str = strstr(body, "true");
            if (pending_str) {
                // Extract verification_id
                char* vid_ptr = strstr(body, "\"verification_id\":");
                int vid = 0;
                if (vid_ptr) {
                    vid = atoi(vid_ptr + 18);
                }
                // Extract student_name
                char student_name[64] = "Unknown";
                char* name_ptr = strstr(body, "\"student_name\":\"");
                if (name_ptr) {
                    name_ptr += 16;
                    char* end = strchr(name_ptr, '"');
                    if (end) {
                        int len = end - name_ptr;
                        if (len > 63) len = 63;
                        strncpy(student_name, name_ptr, len);
                        student_name[len] = '\0';
                    }
                }
                char vid_str[16];
                snprintf(vid_str, sizeof(vid_str), "%d", vid);
                capture_and_send(vid_str, student_name);
                vTaskDelay(pdMS_TO_TICKS(2000));
            }
        }

        vTaskDelay(pdMS_TO_TICKS(POLL_INTERVAL_MS));
    }
}


// ============================================
// Entry point
// ============================================
void app_main(void) {
    ESP_LOGI(TAG, "SEATs S3-EYE starting...");

    // Init NVS
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    init_wifi();
    init_camera();

    if (!wifi_connected) {
        ESP_LOGE(TAG, "WiFi failed — cannot poll API");
        return;
    }

    ESP_LOGI(TAG, "Ready. Polling %s", FLASK_API_URL);

    xTaskCreate(polling_task, "poll_task", 8192, NULL, 5, NULL);
}
