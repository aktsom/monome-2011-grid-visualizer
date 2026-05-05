#!/bin/bash
# monome visualizer — Linux launcher
# automatically routes audio to system output monitor (loopback)

cd "$(dirname "$0")"

# start the visualizer in the background
python3 main.py &
PY_PID=$!

# wait for the audio stream to register with PipeWire/PulseAudio
sleep 3

# find the monitor source (system audio loopback)
MONITOR=$(pactl list sources short | grep -i monitor | awk 'NR==1{print $2}')

if [ -n "$MONITOR" ]; then
    # find the Python recording stream
    STREAM=$(pactl list source-outputs short | grep -i python | awk 'NR==1{print $1}')
    if [ -n "$STREAM" ]; then
        pactl move-source-output "$STREAM" "$MONITOR"
        echo "audio routed to monitor: $MONITOR"
    else
        echo "could not find python audio stream — open pavucontrol to route manually"
    fi
else
    echo "no monitor source found"
fi

# keep script running so the terminal stays open
wait $PY_PID
