// get new values
async function refresh() {
    try {
        const res = await fetch('/api/latest?html=1');
        const data = await res.json();
        const tbody = document.getElementById('tbody');
        const gbody = document.getElementById('gbody');
        tbody.innerHTML = '';
        gbody.innerHTML = '';

        data.rows.forEach(row => {
            const tr = document.createElement('tr');
            const ch = row.channel;

            tr.innerHTML = `
                <td class="ch-cell" data-channel="${ch}">${ch}</td>
                <td>
                    <button id="turn_on_${ch}" onclick="rcTurn(${ch},'on')">ON</button>
                    <button id="turn_off_${ch}" onclick="rcTurn(${ch},'off')">OFF</button>
                </td>
                <td>
                    <button id="acq_en_${ch}" onclick="rcAcq(${ch},'enable')">EN</button>
                    <button id="acq_dis_${ch}" onclick="rcAcq(${ch},'disable')">DIS</button>
                </td>
                <td>
                    <button id="trig_en_${ch}" onclick="rcTrig(${ch},'enable')">EN</button>
                    <button id="trig_dis_${ch}" onclick="rcTrig(${ch},'disable')">DIS</button>
                </td>
                <td>
                    <button id="puls_en_${ch}" onclick="rcPuls(${ch},'enable')">EN</button>
                    <button id="puls_dis_${ch}" onclick="rcPuls(${ch},'disable')">DIS</button>
                </td>
                <td>
                    <button id="hv_on_${ch}" onclick="hvCtrl(${ch},'on')">ON</button>
                    <button id="hv_off_${ch}" onclick="hvCtrl(${ch},'off')">OFF</button>
                </td>
                <td>${row.rate_hz ?? ''}</td>
                <td>${row.rate_th ?? ''}</td>
                <td>${row.ttp.toFixed(1) ?? ''}</td>
                <td>${row.voltage?.toFixed?.(3) ?? ''}</td>
                <td>${row.voltage_set ?? ''}</td>
                <td>${(row.current!=null)?(row.current).toFixed(4):''}</td>
                <td>${row.temperature?.toFixed?.(1) ?? ''}</td>
                <td>${row.status_tag}</td>
                <td>${row.threshold ?? ''}</td>
                <td>${row.alarm_str}</td>
                `;
            tbody.appendChild(tr);

            // --- Apply colors based on API state ---
            updateButtonStyles(ch, row);
            });

        const gr = document.createElement('gr');

        gr.innerHTML = `
            <div class="status-container">

                <section class="status-section">
                    <h4>Global</h4>
                    <div class="status-row">
                        <span class="label">Overcurrent:</span>
                        <span class="value">${data.overcurrent}</span>
                    </div>
                    <div class="status-row">
                        <span class="label">Pulser [Hz]:</span>
                        <span class="value">${data.pulser}</span>
                    </div>
                    <div class="status-row">
                        <span class="label">Data in FIFO:</span>
                        <span class="value">${data.fifo}</span>
                    </div>
                    <div class="status-row">
                        <span class="label">SPI clock frequency [MHz]:</span>
                        <span class="value">${data.spi_freq.toFixed(3)}</span>
                    </div>
                </section>

                <section class="status-section">
                    <h4>Syncronization</h4>
                    <div class="status-row">
                        <span class="label">Tr32:</span>
                        <span class="value">not received, aligned and in synch - counted: 0</span>
                    </div>
                    <div class="status-row">
                        <span class="label">TagT:</span>
                        <span class="value">received (parity OK)</span>
                    </div>
                </section>

                <section class="status-section">
                    <h4>Clock</h4>
                    <div class="status-row">
                        <span class="label">PLL:</span>
                        <span class="value">locked and stable</span>
                    </div>
                    <div class="status-row">
                        <span class="label">Cable 1:</span>
                        <span class="value">not ok, not lost, not found</span>
                    </div>
                    <div class="status-row">
                        <span class="label">Cable 2:</span>
                        <span class="value">not ok, not lost, not found</span>
                    </div>
                    <div class="status-row">
                        <span class="label">Sources:</span>
                        <span class="value">quartz - 1</span>
                    </div>
                </section>

            </div>
            `;
        gbody.appendChild(gr);

    } catch (e) {
    console.error('Refresh error:', e);
    }
}




// plot sensors
async function refreshSensorsLine() {
    const el = document.getElementById("sensor_line");

    try {
        const res = await fetch('/api/sensors/latest');
        const s = await res.json();

        if (!s || !s.ts) {
            el.innerHTML = "Sensors: no data";
            return;
        }

        el.innerHTML = `
        📟 Sensors |
        ⚡ 5V ${s.V_5V?.toFixed?.(3) ?? "-"} V |
        ⚡ 3.3V ${s.V_3V3?.toFixed?.(3) ?? "-"} V |

        🔌 IA ${s.I_poeA?.toFixed?.(3) ?? "-"} A |
        🔌 IB ${s.I_poeB?.toFixed?.(3) ?? "-"} A |

        ⚡ PA ${s.P_poeA?.toFixed?.(2) ?? "-"} W |
        ⚡ PB ${s.P_poeB?.toFixed?.(2) ?? "-"} W |

        🌡 ${s.T?.toFixed?.(2) ?? "-"} °C |
        💧 ${s.H?.toFixed?.(1) ?? "-"} % |
        🌪 ${s.P?.toFixed?.(1) ?? "-"} hPa
        `;
    } catch (e) {
        el.innerHTML = "Sensors: ❌ error";
    }
}

// update live status
async function updateFooterTimestamp() {
    const el = document.getElementById('updated_at');

    try {
        const r = await fetch('/api/last_update');
        const j = await r.json();

        // ---- Timestamp ----
        if (j.updated_at && j.rc.status == "connected") {
            el.textContent = j.updated_at + ' (connected)';
            el.style.color = 'green';
        } else {
            el.textContent = '—';
            el.style.color = 'gray';
        }

        // ---- RunControl status ----
        if (j.rc && j.rc.status !== "connected") {
             el.textContent = j.updated_at + ' ( AcqMainboard disconnected)';
             el.style.color = 'red';
        }

    } catch (e) {
        // API unreachable (Flask down, network error, etc.)
        el.textContent = 'DISCONNECTED';
        el.style.color = 'red';
    }
}

async function getVersion() {
    const el = document.getElementById('firmware_ver');

    try {
        const r = await fetch('/api/firmware');
        const j = await r.json();
        el.textContent = j.version + ' (' + j.date + ')';
    } catch (e) {
        el.textContent = 'v.0.0.0 (01-01-1970)';
    }
}

// --- helper to colorize buttons ---
function updateButtonStyles(ch, row) {
  const activeStyle = 'background:#d1ffd1;border:1px solid #0a0;';
  const inactiveStyle = 'background:#fff;border:1px solid #ccc;';

  // POWER
  const turnOn = document.getElementById(`turn_on_${ch}`);
  const turnOff = document.getElementById(`turn_off_${ch}`);
  if (turnOn && turnOff) {
    turnOn.style = row.turn_on ? activeStyle : inactiveStyle;
    turnOff.style = row.turn_on === false ? activeStyle : inactiveStyle;
  }

  // ENABLE
  const acqEn = document.getElementById(`acq_en_${ch}`);
  const acqDis = document.getElementById(`acq_dis_${ch}`);
  if (acqEn && acqDis) {
    acqEn.style = row.acq_enabled ? activeStyle : inactiveStyle;
    acqDis.style = row.acq_enabled === false ? activeStyle : inactiveStyle;
  }

  // TRIGGER
  const trigEn = document.getElementById(`trig_en_${ch}`);
  const trigDis = document.getElementById(`trig_dis_${ch}`);
  if (trigEn && trigDis) {
    trigEn.style = row.trig_enabled ? activeStyle : inactiveStyle;
    trigDis.style = row.trig_enabled === false ? activeStyle : inactiveStyle;
  }

  // PULSER
  const pulsEn = document.getElementById(`puls_en_${ch}`);
  const pulsDis = document.getElementById(`puls_dis_${ch}`);
  if (pulsEn && pulsDis) {
    pulsEn.style = row.puls_enabled ? activeStyle : inactiveStyle;
    pulsDis.style = row.puls_enabled === false ? activeStyle : inactiveStyle;
  }

  // HV
  const hvOn = document.getElementById(`hv_on_${ch}`);
  const hvOff = document.getElementById(`hv_off_${ch}`);
  if (hvOn && hvOff) {
    hvOn.style = row.hv_on ? activeStyle : inactiveStyle;
    hvOff.style = row.hv_on === false ? activeStyle : inactiveStyle;
  }
}

// --------------------- Controls ---------------------
async function apiPost(url, payload) {
  const res = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {})
  });
  const data = await res.json();
  if(!res.ok){throw new Error(data.error || 'Request failed');}
  return data;
}

function getChosenChannelForControls() {
  const sel = document.getElementById('ch_select').value;
  return sel === 'all' ? 'all' : parseInt(sel);
}

async function setParamHV() {
  const ch = getChosenChannelForControls();
  const param = document.getElementById('hv_param_select').value;
  const val = document.getElementById('hv_param_value').value;
  const msg = document.getElementById('msg');
  msg.textContent = 'Sending...';
  try {
    const out = await apiPost('/api/hv/param', {channel: ch, param: param, value: val});
    msg.textContent = out.message || 'OK';
    refresh();
  } catch(e) {
    msg.textContent = 'Error: ' + e.message;
  }
}

async function setParamRC() {
  const ch = getChosenChannelForControls();
  const param = document.getElementById('rc_param_select').value;
  const val = document.getElementById('rc_param_value').value;
  const msg = document.getElementById('msg');
  msg.textContent = 'Sending...';
  try {
    const out = await apiPost('/api/rc/param', {channel: ch, param: param, value: val});
    msg.textContent = out.message || 'OK';
    refresh();
  } catch(e) {
    msg.textContent = 'Error: ' + e.message;
  }
}

async function hvOn() {
  const ch = getChosenChannelForControls();
  const msg = document.getElementById('msg');
  msg.textContent = 'Sending...';
  try {
    const out = await apiPost('/api/hv/power', {channel: ch, state:'on'});
    msg.textContent = out.message || 'OK';
    refresh();
  } catch(e) {
    msg.textContent = 'Error: ' + e.message;
  }
}
async function hvOff() {
  const ch = getChosenChannelForControls();
  const msg = document.getElementById('msg');
  msg.textContent = 'Sending...';
  try {
    const out = await apiPost('/api/hv/power', {channel: ch, state:'off'});
    msg.textContent = out.message || 'OK';
    refresh();
  } catch(e) {
    msg.textContent = 'Error: ' + e.message;
  }
}
// --------------------- RC controls ---------------------
async function rcTurn(ch, state) {
  try {
    const res = await fetch('/api/rc/power', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({channel: ch, state})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Request failed');
    console.log(`TURN ${state} sent to ch ${ch}`);
  } catch(e) {
    alert('TURN error: ' + e.message);
  }
}

async function rcAcq(ch, action) {
  try {
    const res = await fetch('/api/rc/acq', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({channel: ch, action})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Request failed');
    console.log(`ACQ ${action} sent to ch ${ch}`);
  } catch(e) {
    alert('ACQ error: ' + e.message);
  }
}

async function rcTrig(ch, action) {
  try {
    const res = await fetch('/api/rc/trigger', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({channel: ch, action})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Request failed');
    console.log(`TRIGGER ${action} sent to ch ${ch}`);
  } catch(e) {
    alert('TRIGGER error: ' + e.message);
  }
}

async function rcPuls(ch, action) {
  try {
    const res = await fetch('/api/rc/pulser', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({channel: ch, action})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Request failed');
    console.log(`PULSER ${action} sent to ch ${ch}`);
  } catch(e) {
    alert('PULSER error: ' + e.message);
  }
}

async function hvCtrl(ch, state) {
  try {
    const res = await fetch('/api/hv/power', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({channel: ch, state})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Request failed');
    console.log(`HV ${state} sent to ch ${ch}`);
  } catch(e) {
    alert('HV error: ' + e.message);
  }
}
async function rcTurnAll(action) {
  if (!confirm(`Are you sure you want to TURN ${action.toUpperCase()} all channels?`)) return;
  await fetch('/api/rc/turn_all', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action })
  });
  refresh();
}

async function rcAcqAll(action) {
  if (!confirm(`Are you sure you want to ACQ ${action.toUpperCase()} all channels?`)) return;
  await fetch('/api/rc/acq_all', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action })
  });
  refresh();
}

async function rcTrigAll(action) {
  if (!confirm(`Are you sure you want to TRIGGER ${action.toUpperCase()} all channels?`)) return;
  await fetch('/api/rc/trigger_all', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action })
  });
  refresh();
}

async function rcPulsAll(action) {
  if (!confirm(`Are you sure you want to PULSER ${action.toUpperCase()} all channels?`)) return;
  await fetch('/api/rc/pulser_all', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action })
  });
  refresh();
}

async function hvCtrlAll(action) {
  if (!confirm(`Are you sure you want to HV ${action.toUpperCase()} all channels?`)) return;
  await fetch('/api/hv/all', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action })
  });
  refresh();
}

async function refreshMainboard() {
    const res = await fetch('/api/mainboard');
    const data = await res.json();

    const el = document.getElementById("mainboard_info");

    if (data.temperature != null) {
        el.innerHTML =
            `🔌 Power: ${data.power_ok ? "OK" : "NOT OK"} |
             ⚡ Voltage: ${data.voltage_ok ? "OK" : "NOT OK"}`;
    }
}

function cleanText(str) {
  if (!str) return "";
  // Remove non-printable ASCII characters (0–31) and extended ones (≥127)
  return str.replace(/[^\x20-\x7E]/g, "").trim();
}

setInterval(() => {
  refresh();
  refreshMainboard();
  refreshSensorsLine();
  updateFooterTimestamp();
}, window.APP_CONFIG.refreshMs);

document.addEventListener('DOMContentLoaded', () => {
  refresh();
  refreshMainboard();
  getVersion();
  refreshSensorsLine();
  updateFooterTimestamp();
});