const NAMES = ["Luz", "Música", "Persiana", "Temporizador", "Silencio"];

const circlesEl = document.getElementById("circles");
const displayEl = document.getElementById("display");
const statusEl = document.getElementById("status");
const statusText = document.getElementById("statusText");
const errEl = document.getElementById("err");

const TICKS_PER_STEP = 6;

let index = 0;
let leftover = 0;

const nodes = NAMES.map((name) => {
    const el = document.createElement("div");
    el.className = "circle";
    el.textContent = name;
    circlesEl.appendChild(el);
    return el;
});

function paint() {
    nodes.forEach((el, i) => {
        el.classList.toggle("active", i === index);
    });
}

function step(delta) {
    leftover += delta;
    while (leftover >= TICKS_PER_STEP) {
        leftover -= TICKS_PER_STEP;
        index = (index + 1) % NAMES.length;
    }
    while (leftover <= -TICKS_PER_STEP) {
        leftover += TICKS_PER_STEP;
        index = (index - 1 + NAMES.length) % NAMES.length;
    }
    paint();
}

function punch() {
    displayEl.textContent = NAMES[index];
}

function onMessage(msg) {
    if (msg.type === "hello" && msg.error) {
        errEl.hidden = false;
        errEl.textContent = msg.error;
        return;
    }
    if (msg.type === "error") {
        errEl.hidden = false;
        errEl.textContent = msg.message;
        return;
    }
    if (typeof msg.delta === "number" && msg.delta !== 0) {
        step(msg.delta);
    }
    if (msg.pressed === true) {
        punch();
    }
}

function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + location.host + "/ws");
    ws.onopen = () => {
        statusEl.classList.add("ok");
        statusText.textContent = "conectado";
        ws.send(JSON.stringify({ cmd: "app", name: "selector" }));
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

paint();
connect();
