idf_component_register(SRCS "probe2.c" INCLUDE_DIRS "."
  PRIV_REQUIRES esp_wifi esp_netif esp_event esp_http_client spi_flash esp_timer app_update)
