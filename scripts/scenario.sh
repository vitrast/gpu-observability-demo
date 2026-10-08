#!/usr/bin/env bash
# Inject incidents into the simulated site.
#   ./scripts/scenario.sh power      row power panel trips -> overloaded rack goes dark -> tenant job stalls
#   ./scripts/scenario.sh ups        UPS on battery -> battery drains -> whole feed B lost
#   ./scripts/scenario.sh cooling    CRAH degrades -> hot aisle -> GPU throttling -> slow tenant jobs
#   ./scripts/scenario.sh fabric     one cable/optic degrades -> errors -> flaps -> half speed
#   ./scripts/scenario.sh reset
#   ./scripts/scenario.sh status
SIM=${SIM:-http://localhost:9100}
case "$1" in
  power)   curl -s "$SIM/scenario/rpp_trip?rpp=rpp-r1-b" ;;
  ups)     curl -s "$SIM/scenario/ups_on_battery?ups=ups-b" ;;
  cooling) curl -s "$SIM/scenario/cooling_failure?row=r2" ;;
  fabric)  curl -s "$SIM/scenario/fabric_link?host=gpu-r1-03-n01&gpu=3" ;;
  reset)   curl -s "$SIM/scenario/reset" ;;
  status)  curl -s "$SIM/status" ;;
  *) sed -n '2,8p' "$0"; exit 1 ;;
esac
