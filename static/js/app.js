// get new values
async function refresh() {
    try {
        const res = await fetch('/api/latest?html=1');
        const data = await res.json();
        const tbody = document.getElementById('tbody');
        tbody.innerHTML = '';

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
                <td>
                    <button id="rst_on_${ch}" onclick="rcRst(${ch},'lock')">LOCKED</button>
                    <button id="rst_off_${ch}" onclick="rcRst(${ch},'free')">FREE</button>
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


    } catch (e) {
    console.error('Refresh error:', e);
    }
}

// show sensors data
async function refreshSensorsLine() {
    const el = document.getElementById("sensor-line");

    try {
        const res = await fetch('/api/sensors/latest');
        const s = await res.json();

        if (!s) {
            el.innerHTML = "Sensors: no data";
            return;
        }
        el.innerHTML = `
        <i class="nf nf-fa-wifi"></i>&nbsp;Sensors:&nbsp;<span>&nbsp;</span>
        <i class="nf nf-md-lightning_bolt"></i>&nbsp;5V:${s.V_5V?.toFixed?.(3) ?? "-"} V |&nbsp;
        <i class="nf nf-md-lightning_bolt"></i>&nbsp;3.3V:${s.V_3V3?.toFixed?.(3) ?? "-"} V |&nbsp;

        <i class="nf nf-md-power_plug_outline"></i>&nbsp;IA:${s.I_poeA?.toFixed?.(3) ?? "-"} A |&nbsp;
        <i class="nf nf-md-power_plug_outline"></i>&nbsp;IB:${s.I_poeB?.toFixed?.(3) ?? "-"} A |&nbsp;

        <i class="nf nf-md-lightning_bolt"></i>&nbsp;PA:${s.P_poeA?.toFixed?.(2) ?? "-"} W |&nbsp;
        <i class="nf nf-md-lightning_bolt"></i>&nbsp;PB:${s.P_poeB?.toFixed?.(2) ?? "-"} W |&nbsp;

        <i class="nf nf-fa-temperature_half"></i>&nbsp;${s.T?.toFixed?.(2) ?? "-"} °C |&nbsp;
        <i class="nf nf-weather-humidity"></i>&nbsp;${s.H?.toFixed?.(1) ?? "-"} % |&nbsp;
        <i class="nf nf-md-gauge_full"></i>&nbsp;${s.P?.toFixed?.(1) ?? "-"} hPa
        `;
    } catch (e) {
        el.innerHTML = "Sensors: error";
    }
}

// show DAQ data
async function refreshDAQLine() {
    const el = document.getElementById("daq-line");

    try {
        const res = await fetch('/api/daq/latest');
        const d = await res.json();

        if (!d || d.error) {
            el.innerHTML = "DAQ error";
            return;
        }

        el.innerHTML = `
        <i class="nf nf-fa-database"></i>&nbsp;Data:&nbsp;
        Deadtime: ${d.deadtime}% -
        FIFO: ${d.fifo_words} words (${d.fifo_full ? 'FULL' : 'OK'}) |&nbsp;
        <i class="nf nf-fa-refresh"></i>&nbsp;Timing:&nbsp;
        Tr32: ${d.tr32_received ? '✔' : '✖'}
              (${d.tr32_received
                  ? (d.tr32_aligned ? 'aligned' : 'NOT aligned')
                  : 'NO signal'})
              #${d.tr32_count} -
        TagT: ${d.tagt_received ? '✔' : '✖'}
              (${d.tagt_received
                  ? (d.tagt_aligned ? 'aligned' : 'NOT aligned')
                  : 'NO signal'},
               ${d.tagt_parity_ok ? 'parity OK' : 'PARITY ERR'}) |&nbsp;
        <i class="nf nf-fa-clock"></i>&nbsp;Clock:&nbsp;
        PLL: ${d.pll_locked ? 'locked' : 'FREE'} and
             ${d.pll_stable ? 'stable' : 'UNSTABLE'} -
        Sources: ${d.clock_source} (set: ${d.clock_source_set}) - cable ${d.clock_cable} (set: ${d.clock_cable_set})
        `;

    } catch (e) {
        el.innerHTML = "DAQ error";
    }
}

// show RC data
async function refreshRCLine() {
    const el = document.getElementById("rc-line");

    try {
        const res = await fetch('/api/rc/latest');
        const r = await res.json();

        if (!r || r.error) {
            el.innerHTML = "RunControl error";
            return;
        }

        el.innerHTML = `
        <i class="nf nf-md-controller_classic"></i>&nbsp;RunControl:&nbsp;<span>&nbsp;</span>
        Overcurrent: ${r.overcurrent} |
        SPI speed: ${r.spi_speed.toFixed(3) ?? ''} MHz |
        Pulser freq: ${r.pulser_freq ?? ''} Hz
        `;

    } catch (e) {
        el.innerHTML = "RunControl error";
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
            el.style.color = '#00d4ff';
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

const select = document.getElementById('rc_param_select');
const input = document.getElementById('rc_param_value');
const hint = document.getElementById('input_hint');

select.addEventListener('change', function() {
    if (this.value === 'Spi_speed') {
        // Applica i vincoli per SPI Speed
        input.min = 0;
        input.max = 3;
        input.placeholder = "Range 0-3";
        hint.innerHTML = `Only 0 to 3`;
        hint.style.color = "var(--accent-blue)";
    } else {
        // Rimuovi i vincoli per gli altri parametri
        input.removeAttribute('min');
        input.removeAttribute('max');
        input.placeholder = "Value...";
        hint.innerHTML = "";
    }
});

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
  const activeStyle = 'background:rgba(0,212,255,0.25);border:1px solid #00d4ff;border-radius:4px;';
  const inactiveStyle = 'background:#fff;border:1px solid #ccc;border-radius:4px;';

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

  // RST
  const pulsLock = document.getElementById(`rst_on_${ch}`);
  const pulsFree = document.getElementById(`rst_off_${ch}`);
  if (pulsLock && pulsFree) {
    pulsLock.style = row.rst_enabled ? activeStyle : inactiveStyle;
    pulsFree.style = row.rst_enabled === false ? activeStyle : inactiveStyle;
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

async function rcRst(ch, action) {
  try {
    const res = await fetch('/api/rc/rst', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({channel: ch, action})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Request failed');
    console.log(`${action} sent to ch ${ch}`);
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
  if (!confirm(`Are you sure you want to ENABLE ${action.toUpperCase()} all channels?`)) return;
  await fetch('/api/rc/acq_all', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action })
  });
  refresh();
}

async function rcTrigAll(action) {
  if (!confirm(`Are you sure you want to set TRIGGER ${action.toUpperCase()} all channels?`)) return;
  await fetch('/api/rc/trigger_all', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action })
  });
  refresh();
}

async function rcPulsAll(action) {
  if (!confirm(`Are you sure you want to set PULSER ${action.toUpperCase()} all channels?`)) return;
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

async function rcRstAll(action) {
  if (!confirm(`Are you sure you want to ${action.toUpperCase()} all channels?`)) return;
  await fetch('/api/rc/rst_all', {
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
            `<i class="nf nf-md-power"></i>&nbsp;Power: ${data.power_ok ? "OK" : "NOT OK"} |
             <i class="nf nf-md-lightning_bolt_circle"></i>&nbsp;Voltage: ${data.voltage_ok ? "OK" : "NOT OK"}`;
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
  refreshDAQLine();
  refreshRCLine();
  getVersion();
  updateFooterTimestamp();
}, window.APP_CONFIG.refreshMs);