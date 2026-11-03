// Minimal SolaX PW3 (ESP32-S2) boot probe #2. Delivered via stock MQTT OTA.
// Does NOTHING to flash: no esp_ota_* at startup, no NVS, no partition writes.
// Just brings up WiFi (RAM-only) and POSTs reset_reason + a raw dump of SolaX's
// partition table (0x8000) and bootloader header (0x1000).
//
// WARNING (learned the hard way): this was written assuming the stock
// bootloader has app rollback, so that never marking the image valid would
// fall back to stock on the next reboot. IT DOES NOT. Once this probe boots it
// STICKS across power cycles, and since it has no OTA/command channel the only
// way off it is serial download mode. Do not push it unless you have a serial
// rig ready. It is kept as a minimal example of an IDF 4.4 app that boots under
// the stock bootloader; anything you push for real must carry its own
// update/recovery path (see ../../flasher).
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_flash.h"
#include "esp_idf_version.h"
#include "secrets.h"
static const char *TAG="p2";
static EventGroupHandle_t eg;
static void on_evt(void*a,esp_event_base_t b,int32_t id,void*d){
  if(b==WIFI_EVENT&&id==WIFI_EVENT_STA_START) esp_wifi_connect();
  else if(b==WIFI_EVENT&&id==WIFI_EVENT_STA_DISCONNECTED) esp_wifi_connect();
  else if(b==IP_EVENT&&id==IP_EVENT_STA_GOT_IP) xEventGroupSetBits(eg,1);
}
static void post(const char*path,const void*buf,int len){
  char url[96]; snprintf(url,sizeof url,REPORT "/%s",path);
  esp_http_client_config_t c={.url=url,.method=HTTP_METHOD_POST,.timeout_ms=10000};
  esp_http_client_handle_t h=esp_http_client_init(&c);
  if(esp_http_client_open(h,len)==ESP_OK){ esp_http_client_write(h,buf,len); esp_http_client_fetch_headers(h);}
  esp_http_client_cleanup(h);
}
void app_main(void){
  char info[256]; int n=snprintf(info,sizeof info,"ALIVE idf=%s reset=%d\n",esp_get_idf_version(),esp_reset_reason());
  ESP_LOGI(TAG,"%s",info);
  eg=xEventGroupCreate();
  esp_netif_init(); esp_event_loop_create_default();
  esp_netif_t*nif=esp_netif_create_default_wifi_sta();
  esp_netif_set_hostname(nif,"solax-probe2");
  wifi_init_config_t wc=WIFI_INIT_CONFIG_DEFAULT(); wc.nvs_enable=0;
  esp_wifi_init(&wc); esp_wifi_set_storage(WIFI_STORAGE_RAM);
  esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,on_evt,NULL);
  esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,on_evt,NULL);
  wifi_config_t cfg={0}; strcpy((char*)cfg.sta.ssid,WSSID); strcpy((char*)cfg.sta.password,WPASS);
  esp_wifi_set_mode(WIFI_MODE_STA); esp_wifi_set_config(WIFI_IF_STA,&cfg); esp_wifi_start();
  static uint8_t ptbl[0x1000], bl[0x1000];
  esp_flash_read(NULL,ptbl,0x8000,0x1000);
  esp_flash_read(NULL,bl,0x1000,0x1000);
  for(int i=0;;i++){
    EventBits_t b=xEventGroupWaitBits(eg,1,0,1,pdMS_TO_TICKS(5000));
    if(b&1){
      post("alive",info,n);
      if(i%3==0){ post("ptbl",ptbl,0x1000); post("blhdr",bl,0x100);}
    }
    vTaskDelay(pdMS_TO_TICKS(3000));
  }
}
