#!/bin/bash
# Run in Terminal on the Intel Mac whose identity the bridge will use. Prints
# one line of JSON with what the bridge needs to pose as that Mac: model, en0
# MAC address, serial, platform UUID, root volume UUID, board-id, macOS build,
# ROM (hex) and MLB from NVRAM, the CPU type, the macOS version and the Darwin
# version. Paste that line into the setup app; make-key.py turns it into the
# bridge's hardware key. Changes nothing on the Mac.
#
# Stock macOS tools only (bash 3.2, no python). On Apple Silicon it prints
# {"error": "apple_silicon"}: the bridge can't use those Macs.
# The one-line version of this script is mac-info.oneline.txt. It has no
# single quotes in it, so it can be wrapped in bash -c '...'; keep it that way.
export LC_ALL=C
if [ "$(sysctl -in hw.optional.arm64)" = 1 ]; then echo "{\"error\": \"apple_silicon\"}"; exit 0; fi
# a value of the IOPlatformExpertDevice node, by key
p() { ioreg -rd1 -c IOPlatformExpertDevice | awk -F\" -v k="$1" "\$2==k{print \$4;exit}"; }
# an NVRAM variable in Apple's GUID namespace (nvram prints name, tab, percent-escaped value)
n() { nvram "4D1EDE05-38C7-4A6A-9CC6-4BCCA8B38C14:$1" 2>/dev/null | cut -f2-; }
# ROM: raw bytes; %xx escapes stay as hex, plain characters become their byte value
r=$(n ROM); h=; i=0
while [ "$i" -lt "${#r}" ]; do
  c=${r:$i:1}
  if [ "$c" = % ]; then h=$h${r:$((i+1)):2}; i=$((i+3)); else h=$h$(printf %02x "\"$c"); i=$((i+1)); fi
done
# MLB: plain text, sometimes padded with escaped NUL bytes
m=$(n MLB); m=${m%%\%*}
printf "{\"model\":\"%s\",\"mac\":\"%s\",\"serial\":\"%s\",\"uuid\":\"%s\",\"disk_uuid\":\"%s\",\"board_id\":\"%s\",\"build\":\"%s\",\"rom\":\"%s\",\"mlb\":\"%s\",\"cpu\":\"Intel\",\"version\":\"%s\",\"darwin\":\"%s\"}\n" \
  "$(sysctl -n hw.model)" \
  "$(ifconfig en0 | awk "/ether/{print \$2;exit}")" \
  "$(p IOPlatformSerialNumber)" \
  "$(p IOPlatformUUID)" \
  "$(diskutil info / | awk "/Volume UUID/{print \$NF;exit}")" \
  "$(p board-id)" \
  "$(sw_vers -buildVersion)" \
  "$h" "$m" \
  "$(sw_vers -productVersion)" \
  "$(uname -r)"
