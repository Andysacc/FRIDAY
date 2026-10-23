const TAU = Math.PI * 2;

const MODES = {
  idle: { rgb: [27, 151, 255], speed: 1, energy: 0.08 },
  connecting: { rgb: [82, 190, 255], speed: 2.35, energy: 0.28 },
  listening: { rgb: [22, 209, 255], speed: 1.35, energy: 0.34 },
  speaking: { rgb: [114, 224, 255], speed: 1.8, energy: 0.48 },
};

const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const lerp = (from, to, amount) => from + (to - from) * amount;

function rgba(rgb, alpha) {
  return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
}

function seededNoise(seed) {
  const value = Math.sin(seed * 12.9898 + 78.233) * 43758.5453;
  return value - Math.floor(value);
}

export function createVoiceOrbAnimation(canvas) {
  const context = canvas.getContext("2d", { alpha: true });
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches
    || document.body.classList.contains("reduce-motion");
  const state = {
    mode: "idle",
    energy: 0,
    pointerX: 0,
    pointerY: 0,
    targetPointerX: 0,
    targetPointerY: 0,
    width: 1,
    height: 1,
    dpr: 1,
    visible: true,
  };

  const stars = Array.from({ length: 96 }, (_, index) => ({
    angle: seededNoise(index + 3) * TAU,
    radius: 0.18 + seededNoise(index + 31) * 0.8,
    size: 0.35 + seededNoise(index + 73) * 1.25,
    phase: seededNoise(index + 117) * TAU,
  }));
  const audioEntries = new Map();
  const audioSnapshot = {
    inputLevel: 0,
    outputLevel: 0,
    inputSpectrum: new Float32Array(96),
    outputSpectrum: new Float32Array(96),
    inputConnected: false,
    outputConnected: false,
    mode: "idle",
  };
  let microphoneEntry = null;
  let audioContext = null;
  let animationFrame = 0;
  let previousTime = performance.now();
  let lastReducedFrame = 0;
  let destroyed = false;

  function createAnalyser() {
    const analyser = audioContext.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.76;
    analyser.minDecibels = -88;
    analyser.maxDecibels = -18;
    return {
      analyser,
      data: new Uint8Array(analyser.frequencyBinCount),
      timeData: new Float32Array(analyser.fftSize),
    };
  }

  async function ensureAudioContext() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return null;
    audioContext ||= new AudioContextClass();
    if (audioContext.state === "suspended") await audioContext.resume();
    return audioContext;
  }

  async function attachMediaStream(stream) {
    if (!stream || microphoneEntry?.stream === stream) return;
    detachMediaStream();
    try {
      if (!await ensureAudioContext()) return;
      const source = audioContext.createMediaStreamSource(stream);
      const entry = createAnalyser();
      source.connect(entry.analyser);
      microphoneEntry = { ...entry, source, stream };
    } catch (error) {
      console.warn("FRIDAY voice visualization could not analyze the microphone", error);
    }
  }

  function detachMediaStream() {
    microphoneEntry?.source.disconnect();
    microphoneEntry?.analyser.disconnect();
    microphoneEntry = null;
  }

  async function attachAudioElement(element) {
    if (!element || audioEntries.has(element)) return;
    try {
      if (!await ensureAudioContext()) return;
      const source = audioContext.createMediaElementSource(element);
      const entry = createAnalyser();
      source.connect(entry.analyser);
      entry.analyser.connect(audioContext.destination);
      audioEntries.set(element, { ...entry, source });
    } catch (error) {
      console.warn("FRIDAY voice visualization could not analyze returned audio", error);
    }
  }

  function detachAudioElement(element) {
    const entry = audioEntries.get(element);
    if (!entry) return;
    entry.source.disconnect();
    entry.analyser.disconnect();
    audioEntries.delete(element);
  }

  function sampleEntry(entry) {
    if (!entry) return 0;
    entry.analyser.getByteFrequencyData(entry.data);
    entry.analyser.getFloatTimeDomainData(entry.timeData);
    let sum = 0;
    for (let index = 0; index < entry.timeData.length; index += 1) {
      sum += entry.timeData[index] * entry.timeData[index];
    }
    return clamp(Math.sqrt(sum / Math.max(1, entry.timeData.length)), 0, 1);
  }

  function sampleAudio() {
    let level = sampleEntry(microphoneEntry);
    audioEntries.forEach((entry) => { level = Math.max(level, sampleEntry(entry)); });
    return clamp(level * 4.5, 0, 1);
  }

  function copySpectrum(entry, target, merge = false) {
    if (!entry) {
      if (!merge) target.fill(0);
      return;
    }
    const usefulBins = Math.max(1, Math.floor(entry.data.length * 0.72));
    for (let index = 0; index < target.length; index += 1) {
      const position = index / Math.max(1, target.length - 1);
      const sourceIndex = Math.min(usefulBins - 1, Math.floor(Math.pow(position, 1.48) * usefulBins));
      const value = clamp((entry.data[sourceIndex] / 255) * 1.28, 0, 1);
      target[index] = merge ? Math.max(target[index], value) : value;
    }
  }

  function getAudioSnapshot() {
    const inputLevel = sampleEntry(microphoneEntry);
    audioSnapshot.inputSpectrum.fill(0);
    audioSnapshot.outputSpectrum.fill(0);
    copySpectrum(microphoneEntry, audioSnapshot.inputSpectrum);

    let outputLevel = 0;
    audioEntries.forEach((entry) => {
      outputLevel = Math.max(outputLevel, sampleEntry(entry));
      copySpectrum(entry, audioSnapshot.outputSpectrum, true);
    });

    audioSnapshot.inputLevel = inputLevel;
    audioSnapshot.outputLevel = outputLevel;
    audioSnapshot.inputConnected = Boolean(microphoneEntry);
    audioSnapshot.outputConnected = audioEntries.size > 0;
    audioSnapshot.mode = state.mode;
    return audioSnapshot;
  }

  function resize() {
    const bounds = canvas.getBoundingClientRect();
    state.width = Math.max(1, Math.floor(bounds.width));
    state.height = Math.max(1, Math.floor(bounds.height));
    state.dpr = Math.min(window.devicePixelRatio || 1, state.width < 420 ? 1.5 : 2);
    canvas.width = Math.round(state.width * state.dpr);
    canvas.height = Math.round(state.height * state.dpr);
    context.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
  }

  function arc(radius, start, end, color, width = 1) {
    context.beginPath();
    context.arc(0, 0, radius, start, end);
    context.strokeStyle = color;
    context.lineWidth = width;
    context.stroke();
  }

  function segmentedRing(radius, count, coverage, rotation, rgb, alpha, width = 1) {
    const step = TAU / count;
    for (let index = 0; index < count; index += 1) {
      if (seededNoise(index + radius) < 0.12) continue;
      const start = rotation + index * step;
      const variance = 0.7 + seededNoise(index * 9 + radius) * 0.3;
      arc(radius, start, start + step * coverage * variance, rgba(rgb, alpha), width);
    }
  }

  function drawStars(radius, time, rgb, activity) {
    for (let index = 0; index < stars.length; index += 1) {
      const star = stars[index];
      const orbit = radius * star.radius;
      const angle = star.angle + time * (0.012 + (index % 5) * 0.002);
      const x = Math.cos(angle) * orbit;
      const y = Math.sin(angle) * orbit * 0.94;
      const twinkle = 0.16 + (Math.sin(time * 2 + star.phase) + 1) * 0.13;
      context.fillStyle = rgba(rgb, twinkle + activity * 0.18);
      context.fillRect(x, y, star.size, star.size);
    }
  }

  function drawRadialFrame(radius, time, rgb, activity) {
    context.save();
    context.rotate(-time * 0.016);
    for (let index = 0; index < 24; index += 1) {
      const angle = index * TAU / 24;
      const inner = radius * (index % 3 === 0 ? 0.36 : 0.49);
      const outer = radius * (0.7 + seededNoise(index + 8) * 0.22);
      context.beginPath();
      context.moveTo(Math.cos(angle) * inner, Math.sin(angle) * inner);
      context.lineTo(Math.cos(angle) * outer, Math.sin(angle) * outer);
      context.strokeStyle = rgba(rgb, index % 3 === 0 ? 0.18 + activity * 0.15 : 0.075);
      context.lineWidth = index % 3 === 0 ? 0.9 : 0.55;
      context.stroke();
    }
    context.restore();
  }

  function drawScan(radius, time, rgb, activity) {
    const scanAngle = time * (state.mode === "connecting" ? 1.35 : 0.38);
    const gradient = context.createConicGradient(scanAngle, 0, 0);
    gradient.addColorStop(0, rgba(rgb, 0));
    gradient.addColorStop(0.045, rgba(rgb, 0.1 + activity * 0.18));
    gradient.addColorStop(0.08, rgba(rgb, 0));
    gradient.addColorStop(1, rgba(rgb, 0));
    context.beginPath();
    context.arc(0, 0, radius * 0.83, 0, TAU);
    context.fillStyle = gradient;
    context.fill();
  }

  function drawTicks(radius, time, rgb, speed) {
    context.save();
    context.rotate(time * 0.026 * speed);
    for (let index = 0; index < 96; index += 1) {
      const angle = index * TAU / 96;
      const isMajor = index % 8 === 0;
      const inner = radius * (isMajor ? 0.855 : 0.875);
      const outer = radius * (isMajor ? 0.91 : 0.895);
      context.beginPath();
      context.moveTo(Math.cos(angle) * inner, Math.sin(angle) * inner);
      context.lineTo(Math.cos(angle) * outer, Math.sin(angle) * outer);
      context.strokeStyle = rgba(rgb, isMajor ? 0.47 : 0.15);
      context.lineWidth = isMajor ? 1.15 : 0.55;
      context.stroke();
    }
    context.restore();
  }

  function drawStructuralNodes(radius, time, rgb, energy, speed) {
    const nodes = 12;
    const nodeRadius = radius * 0.68;
    context.save();
    context.rotate(-time * 0.055 * speed);
    for (let index = 0; index < nodes; index += 1) {
      const angle = index * TAU / nodes;
      const x = Math.cos(angle) * nodeRadius;
      const y = Math.sin(angle) * nodeRadius;
      const innerX = Math.cos(angle) * radius * 0.36;
      const innerY = Math.sin(angle) * radius * 0.36;
      context.beginPath();
      context.moveTo(innerX, innerY);
      context.lineTo(x, y);
      context.strokeStyle = rgba(rgb, index % 3 === 0 ? 0.34 : 0.13);
      context.lineWidth = index % 3 === 0 ? 1 : 0.65;
      context.stroke();

      if (index % 2 === 0) {
        context.shadowColor = rgba(rgb, 0.95);
        context.shadowBlur = 8 + energy * 12;
        context.beginPath();
        context.arc(x, y, 2.2 + energy * 1.8, 0, TAU);
        context.fillStyle = rgba(rgb, 0.86);
        context.fill();
        context.shadowBlur = 0;
        arc(nodeRadius, angle - 0.035, angle + 0.035, rgba(rgb, 0.68), 2.1);
      }
    }
    context.restore();
  }

  function drawVoiceMembrane(radius, time, rgb, energy) {
    const points = 96;
    const base = radius * 0.34;
    context.beginPath();
    for (let index = 0; index <= points; index += 1) {
      const angle = index * TAU / points;
      const harmonic = Math.sin(angle * 6 - time * 4.2) * 0.5
        + Math.sin(angle * 11 + time * 2.7) * 0.3
        + Math.sin(angle * 17 - time * 5.1) * 0.2;
      const response = base + harmonic * radius * (0.012 + energy * 0.07);
      const x = Math.cos(angle) * response;
      const y = Math.sin(angle) * response;
      if (index === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    }
    context.closePath();
    context.strokeStyle = rgba(rgb, 0.5 + energy * 0.42);
    context.lineWidth = 1 + energy * 1.4;
    context.shadowColor = rgba(rgb, 0.9);
    context.shadowBlur = 7 + energy * 14;
    context.stroke();
    context.shadowBlur = 0;
  }

  function drawCore(radius, time, rgb, energy) {
    const pulse = 1 + Math.sin(time * 3.2) * 0.035 + energy * 0.22;
    const coreRadius = radius * 0.085 * pulse;
    const aura = context.createRadialGradient(0, 0, 0, 0, 0, radius * (0.34 + energy * 0.08));
    aura.addColorStop(0, "rgba(226, 251, 255, .98)");
    aura.addColorStop(0.055, rgba(rgb, 0.92));
    aura.addColorStop(0.19, rgba(rgb, 0.28 + energy * 0.18));
    aura.addColorStop(0.52, rgba(rgb, 0.075));
    aura.addColorStop(1, rgba(rgb, 0));
    context.fillStyle = aura;
    context.beginPath();
    context.arc(0, 0, radius * 0.42, 0, TAU);
    context.fill();

    context.shadowColor = rgba(rgb, 1);
    context.shadowBlur = 22 + energy * 32;
    context.fillStyle = "rgba(228, 252, 255, .98)";
    context.beginPath();
    context.arc(0, 0, coreRadius, 0, TAU);
    context.fill();
    context.shadowBlur = 0;
    arc(radius * 0.16, 0, TAU, rgba(rgb, 0.65), 1.2);
    arc(radius * 0.205, 0, TAU, rgba(rgb, 0.24), 0.7);
  }

  function draw(timeMs) {
    const delta = Math.min(0.05, (timeMs - previousTime) / 1000);
    previousTime = timeMs;
    const time = timeMs / 1000;
    const mode = MODES[state.mode];
    const measured = sampleAudio();
    const synthetic = state.mode === "connecting"
      ? 0.18 + (Math.sin(time * 5.5) + 1) * 0.08
      : state.mode === "listening" ? 0.12 + (Math.sin(time * 2.2) + 1) * 0.035 : 0;
    const targetEnergy = Math.max(mode.energy, measured, synthetic);
    state.energy = lerp(state.energy, targetEnergy, targetEnergy > state.energy ? 0.2 : 0.065);
    state.pointerX = lerp(state.pointerX, state.targetPointerX, 0.045);
    state.pointerY = lerp(state.pointerY, state.targetPointerY, 0.045);

    context.clearRect(0, 0, state.width, state.height);
    const centerX = state.width * 0.5 + state.pointerX * 10;
    const centerY = state.height * 0.48 + state.pointerY * 8;
    const radius = Math.min(state.width * 0.47, state.height * 0.49, 310);
    const rgb = mode.rgb;
    const animationTime = reducedMotion ? time * 0.08 : time;
    const speed = reducedMotion ? 0.08 : mode.speed;

    context.save();
    context.translate(centerX, centerY);
    context.globalCompositeOperation = "lighter";
    drawStars(radius, animationTime, rgb, state.energy);
    drawScan(radius, animationTime * speed, rgb, state.energy);
    drawRadialFrame(radius, animationTime, rgb, state.energy);

    segmentedRing(radius * 0.93, 32, 0.67, animationTime * 0.028 * speed, rgb, 0.24, 0.75);
    segmentedRing(radius * 0.88, 14, 0.54, -animationTime * 0.05 * speed, rgb, 0.48, 1.1);
    segmentedRing(radius * 0.78, 20, 0.72, animationTime * 0.08 * speed, rgb, 0.23, 0.8);
    segmentedRing(radius * 0.57, 12, 0.64, -animationTime * 0.12 * speed, rgb, 0.52, 1.15);
    segmentedRing(radius * 0.46, 18, 0.74, animationTime * 0.16 * speed, rgb, 0.29, 0.75);
    drawTicks(radius, animationTime, rgb, speed);
    drawStructuralNodes(radius, animationTime, rgb, state.energy, speed);
    drawVoiceMembrane(radius, animationTime, rgb, state.energy);
    drawCore(radius, animationTime, rgb, state.energy);
    context.restore();

    if (delta > 0.049) state.energy *= 0.985;
  }

  function animate(time) {
    if (destroyed) return;
    animationFrame = requestAnimationFrame(animate);
    if (!state.visible) return;
    if (reducedMotion && time - lastReducedFrame < 90) return;
    lastReducedFrame = time;
    draw(time);
  }

  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(canvas);
  const intersectionObserver = new IntersectionObserver(([entry]) => {
    state.visible = entry.isIntersecting;
  });
  intersectionObserver.observe(canvas);
  resize();
  animationFrame = requestAnimationFrame(animate);

  return {
    setMode(mode) {
      state.mode = MODES[mode] ? mode : "idle";
    },
    setPointer(x, y) {
      state.targetPointerX = clamp(x, -1, 1);
      state.targetPointerY = clamp(y, -1, 1);
    },
    attachMediaStream,
    detachMediaStream,
    attachAudioElement,
    detachAudioElement,
    getAudioSnapshot,
    resumeAudio() {
      return audioContext?.state === "suspended" ? audioContext.resume() : Promise.resolve();
    },
    destroy() {
      destroyed = true;
      cancelAnimationFrame(animationFrame);
      resizeObserver.disconnect();
      intersectionObserver.disconnect();
      detachMediaStream();
      [...audioEntries.keys()].forEach(detachAudioElement);
      audioContext?.close();
    },
  };
}
