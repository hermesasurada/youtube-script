#!/bin/zsh
# hermes 크론 진입점: youtube-monitor 아웃박스의 새 알림을 stdout으로 배출 → 텔레그램 배달.
# 무거운 폴/드레인은 별도 launchd(com.yhandhs.youtube-monitor)가 담당. 이 shim은 알림만(빠름).
exec "$HOME/youtube-script/.venv/bin/python" "$HOME/youtube-script/channel_monitor.py" --announce
