const wrap = document.getElementById("wrap");
const knob = document.getElementById("knob");
const statusEl = document.getElementById("status");
const statusText = document.getElementById("statusText");
const brightnessEl = document.getElementById("brightness");
const pulseEl = document.getElementById("pulse");
const angleReadout = document.getElementById("angleReadout");
const deviceEl = document.getElementById("device");
const errEl = document.getElementById("err");
const glow = document.getElementById("glow");

let angle = 0;
let brightness = 128;
let pressed = false;

function renderKnob() {
    const scale = pressed ? 0.97 : 1;
    knob.style.transform = "rotate(" + angle + "deg) scale(" + scale + ")";
    angleReadout.textContent = Math.round(angle) + "°";
}

function setAngle(delta) {
    angle += Number(delta) * (360 / 94);
    renderKnob();
}

function setLedVisual(state) {
    if (!state || typeof state.brightness !== "number") {
        return;
    }
    brightness = state.brightness;
    wrap.classList.toggle("pulse", !!state.pulse);
    glow.style.opacity = String(state.pulse ? 0.28 : 0.08 + (state.brightness / 255) * 0.5);
    brightnessEl.textContent = state.brightness;
    pulseEl.textContent = state.pulse ? (state.pulseLevel + "%") : "off";
}

function showError(text) {
    errEl.hidden = false;
    errEl.textContent = text;
}

function onMessage(msg) {
    if (msg.type === "hello") {
        deviceEl.textContent = msg.device || "sin dispositivo";
        if (msg.error) {
            showError(msg.error);
        }
        setLedVisual(msg);
        return;
    }
    if (msg.type === "error") {
        showError(msg.message);
        return;
    }
    if (typeof msg.delta === "number" && msg.delta !== 0) {
        setAngle(msg.delta);
    }
    if (typeof msg.pressed === "boolean") {
        pressed = msg.pressed;
        wrap.classList.toggle("down", pressed);
        renderKnob();
    }
    setLedVisual(msg);
}

function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + location.host + "/ws");
    ws.onopen = () => {
        statusEl.classList.add("ok");
        statusText.textContent = "conectado";
        ws.send(JSON.stringify({ cmd: "app", name: "monitor" }));
    };
    ws.onclose = () => {
        statusEl.classList.remove("ok");
        statusText.textContent = "desconectado";
        setTimeout(connect, 1200);
    };
    ws.onerror = () => {
        statusText.textContent = "error";
    };
    ws.onmessage = (ev) => {
        onMessage(JSON.parse(ev.data));
    };
}

renderKnob();
connect();
