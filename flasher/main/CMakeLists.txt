idf_component_register(SRCS "flasher.c" INCLUDE_DIRS "."
  PRIV_REQUIRES esp_wifi esp_netif esp_event esp_http_client spi_flash mbedtls)
