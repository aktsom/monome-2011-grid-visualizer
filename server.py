"""server.py — Flask web UI for the monome visualizer."""
import logging
import sys
import time
from flask import Flask, jsonify, request, render_template
import sounddevice as sd
import state

# suppress per-request werkzeug logs
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    return jsonify(state.snapshot())


_NAME_BLOCKLIST  = ('mapper', 'primary sound', 'communications')
if sys.platform == 'win32':
    _ALLOWED_APIS = ('asio', 'wasapi')
elif sys.platform == 'darwin':
    _ALLOWED_APIS = ('core audio',)
else:
    _ALLOWED_APIS = ('alsa', 'jack', 'pulseaudio', 'pipewire')

@app.route('/api/audio-devices')
def api_audio_devices():
    show_all = request.args.get('all', '0') == '1'
    hostapis = sd.query_hostapis()
    devices  = sd.query_devices()
    seen     = set()
    inputs   = []
    for i, d in enumerate(devices):
        if d['max_input_channels'] < 2:
            continue
        api = hostapis[d['hostapi']]['name']
        if not show_all and not any(a in api.lower() for a in _ALLOWED_APIS):
            continue
        nl = d['name'].lower()
        if any(bl in nl for bl in _NAME_BLOCKLIST):
            continue
        label = f"{d['name']} ({api})"
        if label in seen:
            continue
        seen.add(label)
        inputs.append({'index': i, 'name': label})
    return jsonify(inputs)


@app.route('/api/grids')
def api_grids():
    return jsonify(state.get_grids())


@app.route('/api/presets')
def api_presets():
    return jsonify(state.PRESETS)


@app.route('/api/settings', methods=['POST'])
def api_settings():
    data = request.get_json()
    if 'gain' in data:
        state.gain = float(data['gain'])
        state.preset_gain[state.preset] = state.gain
        state.save_config()
    if 'preset' in data and data['preset'] in state.PRESETS:
        state.preset = data['preset']
        state.gain   = state.preset_gain[state.preset]
        state.save_config()
    if 'audio_device' in data:
        state.audio_device  = int(data['audio_device'])
        state.restart_audio = True
        # config saved by main loop after stream restarts successfully
    return jsonify({'ok': True})


@app.route('/api/heartbeat', methods=['POST'])
def api_heartbeat():
    state.last_heartbeat = time.time()
    return jsonify({'ok': True})


@app.route('/api/trim', methods=['GET'])
def api_trim_get():
    return jsonify(state.band_trim)


@app.route('/api/trim', methods=['POST'])
def api_trim_post():
    data = request.get_json()
    name   = data.get('preset')
    values = data.get('values')
    if name in state.band_trim and isinstance(values, list):
        if len(values) == len(state.band_trim[name]):
            state.band_trim[name] = [max(0.0, min(2.0, float(v))) for v in values]
            state.save_config()
    return jsonify({'ok': True})


@app.route('/api/led-roles', methods=['GET'])
def api_led_roles_get():
    return jsonify(state.led_roles)


@app.route('/api/led-roles', methods=['POST'])
def api_led_roles_post():
    data  = request.get_json()
    name  = data.get('preset')
    roles = data.get('roles')
    if name in state.led_roles and isinstance(roles, dict):
        for role, val in roles.items():
            if role in state.led_roles[name]:
                state.led_roles[name][role] = max(1, min(15, int(val)))
        state.save_config()
    return jsonify({'ok': True})


@app.route('/api/col-brightness', methods=['GET'])
def api_col_brightness_get():
    return jsonify(state.col_brightness)


@app.route('/api/col-brightness', methods=['POST'])
def api_col_brightness_post():
    data   = request.get_json()
    name   = data.get('preset')
    values = data.get('values')
    if name in state.col_brightness and isinstance(values, list):
        if len(values) == len(state.col_brightness[name]):
            state.col_brightness[name] = [max(0.0, min(1.0, float(v))) for v in values]
            state.save_config()
    return jsonify({'ok': True})


@app.route('/api/connect-grid', methods=['POST'])
def api_connect_grid():
    data = request.get_json()
    grid_id = data.get('id')
    grids = state.get_grids()
    if grid_id in grids:
        state.reconnect_grid = grids[grid_id]['port']
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'grid not found'}), 404


def run(host='0.0.0.0', port=5000):
    app.run(host=host, port=port, debug=False, use_reloader=False)
