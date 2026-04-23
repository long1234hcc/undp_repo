import React, { useState, useEffect, useRef, useCallback } from 'react';
import DeckGL from '@deck.gl/react';
import { PolygonLayer, GeoJsonLayer } from '@deck.gl/layers';
import Map from 'react-map-gl/maplibre';
import 'maplibre-gl/dist/maplibre-gl.css';
import axios from 'axios';

// ─── Constants ────────────────────────────────────────────────────────────────

const INITIAL_VIEW_STATE = {
  longitude: 112.0,
  latitude: 12.0,
  zoom: 4,
  pitch: 0,
  bearing: 0,
};

// ─── Turbo Colormap (7 stops) ─────────────────────────────────────────────────
// Chuyển risk [0..1] → RGB theo bảng màu khoa học Turbo
function turboColor(t) {
  // 7 stops: Deep Blue → Cyan → Green → Yellow → Orange → Red → Dark Red
  const stops = [
    [0.00, [17,   95,  154]],
    [0.17, [25,  180,  220]],
    [0.33, [80,  220,  100]],
    [0.50, [255, 230,   20]],
    [0.67, [255, 140,    0]],
    [0.83, [220,   0,    0]],
    [1.00, [100,   0,   30]],
  ];
  const clamped = Math.min(Math.max(t, 0), 1);
  for (let i = 1; i < stops.length; i++) {
    const [t0, c0] = stops[i - 1];
    const [t1, c1] = stops[i];
    if (clamped <= t1) {
      const f = (clamped - t0) / (t1 - t0);
      return [
        Math.round(c0[0] + f * (c1[0] - c0[0])),
        Math.round(c0[1] + f * (c1[1] - c0[1])),
        Math.round(c0[2] + f * (c1[2] - c0[2])),
      ];
    }
  }
  return stops[stops.length - 1][1];
}

// ─── Utility ──────────────────────────────────────────────────────────────────

const formatTime = (isoString) => {
  if (!isoString) return '';
  const d = new Date(isoString);
  return d.toLocaleString('vi-VN', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
};

const formatShortDate = (isoString) => {
  if (!isoString) return '';
  const d = new Date(isoString);
  return `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}`;
};

// ─── Sub-components ───────────────────────────────────────────────────────────

function WeightSlider({ icon, label, subLabel, value, onChange, accentColor, trackGradient }) {
  const pct = value * 100;
  return (
    <div style={{ marginBottom: 20 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <span style={{ fontSize: 16 }}>{icon}</span>
          <div>
            <div style={{ fontSize: 12, fontWeight: 600, color: '#e2e8f0', letterSpacing: '0.02em' }}>{label}</div>
            <div style={{ fontSize: 10, color: '#64748b', marginTop: 1 }}>{subLabel}</div>
          </div>
        </div>
        <div style={{
          background: accentColor + '22', border: `1px solid ${accentColor}55`,
          borderRadius: 6, padding: '3px 10px',
          fontFamily: "'JetBrains Mono', 'Courier New', monospace",
          fontSize: 13, fontWeight: 700, color: accentColor,
          minWidth: 48, textAlign: 'center',
        }}>
          {value.toFixed(2)}
        </div>
      </div>
      <div style={{ position: 'relative', height: 28, display: 'flex', alignItems: 'center' }}>
        <div style={{ position: 'absolute', left: 0, right: 0, height: 6, borderRadius: 3, background: trackGradient, opacity: 0.3 }} />
        <div style={{ position: 'absolute', left: 0, width: `${pct}%`, height: 6, borderRadius: 3, background: trackGradient }} />
        <input
          type="range" min={0} max={1} step={0.05} value={value}
          onChange={e => onChange(parseFloat(e.target.value))}
          style={{ position: 'absolute', left: 0, right: 0, width: '100%', height: 28, opacity: 0, cursor: 'pointer', margin: 0, zIndex: 2 }}
        />
        <div style={{
          position: 'absolute', left: `calc(${pct}% - 10px)`,
          width: 20, height: 20, borderRadius: '50%',
          background: '#0f172a', border: `2.5px solid ${accentColor}`,
          boxShadow: `0 0 0 3px ${accentColor}33, 0 2px 8px rgba(0,0,0,0.5)`,
          pointerEvents: 'none', transition: 'left 0.05s',
        }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 3 }}>
        <span style={{ fontSize: 10, color: '#475569' }}>Thấp</span>
        <span style={{ fontSize: 10, color: '#475569' }}>Cao</span>
      </div>
    </div>
  );
}

function RiskLegend() {
  const stops = ['#115f9a', '#1ebbd7', '#50dc64', '#ffe614', '#ff8c00', '#dc0000', '#640020'];
  const gradient = `linear-gradient(to right, ${stops.join(', ')})`;
  return (
    <div style={{
      background: 'rgba(8,14,28,0.88)', backdropFilter: 'blur(12px)',
      border: '1px solid rgba(255,255,255,0.08)', borderRadius: 12,
      padding: '10px 14px', width: 220,
    }}>
      <div style={{ fontSize: 10, fontWeight: 700, color: '#64748b', letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 7 }}>
        Chỉ số rủi ro
      </div>
      <div style={{ height: 10, borderRadius: 5, background: gradient, marginBottom: 5 }} />
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span style={{ fontSize: 10, color: '#475569' }}>0.0 — Thấp</span>
        <span style={{ fontSize: 10, color: '#475569' }}>1.0 — Cao</span>
      </div>
    </div>
  );
}

function HoverTooltip({ info, weightTemp, weightHum }) {
  if (!info?.object) return null;
  const obj = info.object;
  const risk = Math.min(Math.max((obj.nT * weightTemp) + (obj.nH * weightHum), 0), 1);
  const tempC = (obj.nT * 35 + 10).toFixed(1);
  const humPct = (obj.nH * 80 + 20).toFixed(1);
  const riskColor = risk > 0.75 ? '#ef4444' : risk > 0.5 ? '#f97316' : risk > 0.25 ? '#eab308' : '#22c55e';

  return (
    <div style={{
      position: 'absolute', left: info.x, top: info.y,
      transform: 'translate(-50%, -115%)', pointerEvents: 'none', zIndex: 100, minWidth: 200,
    }}>
      <div style={{
        position: 'absolute', bottom: -6, left: '50%', transform: 'translateX(-50%)',
        width: 0, height: 0,
        borderLeft: '6px solid transparent', borderRight: '6px solid transparent',
        borderTop: '6px solid rgba(8,14,28,0.95)',
      }} />
      <div style={{
        background: 'rgba(8,14,28,0.95)', backdropFilter: 'blur(16px)',
        border: '1px solid rgba(255,255,255,0.1)', borderRadius: 12,
        padding: '12px 14px', boxShadow: '0 20px 60px rgba(0,0,0,0.5)',
      }}>
        <div style={{ fontSize: 10, color: '#475569', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 8 }}>
          📍 {parseFloat(obj.lat).toFixed(2)}°N, {parseFloat(obj.lon).toFixed(2)}°E
        </div>
        <div style={{ display: 'flex', gap: 12, marginBottom: 10 }}>
          <div style={{ flex: 1, background: 'rgba(251,191,36,0.08)', border: '1px solid rgba(251,191,36,0.2)', borderRadius: 8, padding: '7px 10px' }}>
            <div style={{ fontSize: 9, color: '#92400e', fontWeight: 700, letterSpacing: '0.06em', marginBottom: 3 }}>🌡 NHIỆT ĐỘ</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: '#fbbf24', lineHeight: 1 }}>{tempC}<span style={{ fontSize: 11, fontWeight: 500, marginLeft: 2 }}>°C</span></div>
          </div>
          <div style={{ flex: 1, background: 'rgba(34,211,238,0.08)', border: '1px solid rgba(34,211,238,0.2)', borderRadius: 8, padding: '7px 10px' }}>
            <div style={{ fontSize: 9, color: '#164e63', fontWeight: 700, letterSpacing: '0.06em', marginBottom: 3 }}>💧 ĐỘ ẨM</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: '#22d3ee', lineHeight: 1 }}>{humPct}<span style={{ fontSize: 11, fontWeight: 500, marginLeft: 2 }}>%</span></div>
          </div>
        </div>
        <div style={{ background: riskColor + '15', border: `1px solid ${riskColor}40`, borderRadius: 8, padding: '8px 10px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 5 }}>
            <span style={{ fontSize: 10, color: '#94a3b8', fontWeight: 600 }}>RISK SCORE</span>
            <span style={{ fontSize: 16, fontWeight: 800, color: riskColor }}>{risk.toFixed(3)}</span>
          </div>
          <div style={{ height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.08)' }}>
            <div style={{ width: `${risk * 100}%`, height: '100%', borderRadius: 2, background: `linear-gradient(to right, #22c55e, #eab308, #f97316, #ef4444)` }} />
          </div>
        </div>
      </div>
    </div>
  );
}

function ControlPanel({ weightTemp, setWeightTemp, weightHum, setWeightHum }) {
  const total = weightTemp + weightHum;
  const riskLevel = total > 1.5 ? { label: 'RẤT CAO', color: '#ef4444' }
    : total > 1.0 ? { label: 'CAO', color: '#f97316' }
    : total > 0.5 ? { label: 'TRUNG BÌNH', color: '#eab308' }
    : { label: 'THẤP', color: '#22c55e' };
  const tPct = (weightTemp / (total || 1) * 100).toFixed(0);
  const hPct = (weightHum / (total || 1) * 100).toFixed(0);

  return (
    <div style={{
      position: 'absolute', top: 20, right: 20, zIndex: 10, width: 300,
      background: 'rgba(8,14,28,0.90)', backdropFilter: 'blur(20px)',
      border: '1px solid rgba(255,255,255,0.07)', borderRadius: 16, padding: '20px',
      boxShadow: '0 25px 80px rgba(0,0,0,0.6)',
    }}>
      <div style={{ marginBottom: 20, paddingBottom: 16, borderBottom: '1px solid rgba(255,255,255,0.07)' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: 11, fontWeight: 700, color: '#475569', letterSpacing: '0.12em', textTransform: 'uppercase', marginBottom: 4 }}>UNDP · Risk Evaluation</div>
            <div style={{ fontSize: 18, fontWeight: 700, color: '#f1f5f9', lineHeight: 1 }}>Weather Risk</div>
          </div>
          <div style={{ background: riskLevel.color + '20', border: `1px solid ${riskLevel.color}50`, borderRadius: 8, padding: '6px 10px', textAlign: 'center' }}>
            <div style={{ fontSize: 8, color: riskLevel.color, fontWeight: 700, letterSpacing: '0.06em', marginBottom: 2 }}>TỔNG RỦI RO</div>
            <div style={{ fontSize: 11, fontWeight: 800, color: riskLevel.color }}>{riskLevel.label}</div>
          </div>
        </div>
      </div>
      <WeightSlider icon="🌡" label="Nhiệt độ" subLabel="Temperature weight" value={weightTemp} onChange={setWeightTemp} accentColor="#f59e0b" trackGradient="linear-gradient(to right, #1d4ed8, #16a34a, #ca8a04, #dc2626)" />
      <WeightSlider icon="💧" label="Độ ẩm" subLabel="Humidity weight" value={weightHum} onChange={setWeightHum} accentColor="#22d3ee" trackGradient="linear-gradient(to right, #f5f3e8, #93c5fd, #0284c7)" />
      <div style={{ marginTop: 4 }}>
        <div style={{ fontSize: 10, color: '#475569', fontWeight: 600, marginBottom: 6, display: 'flex', justifyContent: 'space-between' }}>
          <span>Tỷ lệ trọng số</span>
          <span style={{ color: '#334155' }}>T: {tPct}% · H: {hPct}%</span>
        </div>
        <div style={{ height: 6, borderRadius: 3, background: 'rgba(255,255,255,0.06)', overflow: 'hidden', display: 'flex' }}>
          <div style={{ flex: weightTemp, background: 'linear-gradient(to right, #f59e0b, #ef4444)', transition: 'flex 0.3s ease' }} />
          <div style={{ flex: weightHum, background: 'linear-gradient(to right, #0ea5e9, #22d3ee)', transition: 'flex 0.3s ease' }} />
        </div>
      </div>
      <p style={{ marginTop: 16, fontSize: 10, color: '#334155', lineHeight: 1.6, fontStyle: 'italic', borderTop: '1px solid rgba(255,255,255,0.05)', paddingTop: 12, marginBottom: 0 }}>
        Kéo thanh trượt để điều chỉnh mức độ ưu tiên. Bản đồ cập nhật theo thời gian thực.
      </p>
    </div>
  );
}

function TimelineBar({ timeList, currentTimeIndex, setCurrentTimeIndex, loading }) {
  const [isPlaying, setIsPlaying] = useState(false);
  const playRef = useRef(null);

  useEffect(() => {
    if (isPlaying) {
      playRef.current = setInterval(() => {
        setCurrentTimeIndex(prev => {
          if (prev >= timeList.length - 1) { setIsPlaying(false); return prev; }
          return prev + 1;
        });
      }, 600);
    } else {
      clearInterval(playRef.current);
    }
    return () => clearInterval(playRef.current);
  }, [isPlaying, timeList.length, setCurrentTimeIndex]);

  if (timeList.length === 0) return null;
  const isLive = currentTimeIndex === timeList.length - 1;
  const pct = (currentTimeIndex / Math.max(timeList.length - 1, 1)) * 100;

  // Hiển thị tối đa 20 tick marks để không bị chật
  const tickStep = Math.ceil(timeList.length / 20);

  return (
    <div style={{
      position: 'absolute', bottom: 24, left: '50%', transform: 'translateX(-50%)',
      zIndex: 10, width: '88%', maxWidth: 860,
      background: 'rgba(8,14,28,0.92)', backdropFilter: 'blur(20px)',
      border: '1px solid rgba(255,255,255,0.07)', borderRadius: 16,
      padding: '14px 20px', boxShadow: '0 20px 60px rgba(0,0,0,0.5)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <button
            onClick={() => setIsPlaying(p => !p)}
            style={{
              width: 32, height: 32, borderRadius: '50%',
              background: isPlaying ? 'rgba(239,68,68,0.15)' : 'rgba(99,102,241,0.15)',
              border: `1px solid ${isPlaying ? 'rgba(239,68,68,0.4)' : 'rgba(99,102,241,0.4)'}`,
              color: isPlaying ? '#ef4444' : '#818cf8',
              cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 12, transition: 'all 0.2s',
            }}
          >
            {isPlaying ? '⏸' : '▶'}
          </button>
          <div>
            <div style={{ fontSize: 10, color: '#475569', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase' }}>Thời điểm đánh giá</div>
            <div style={{ fontSize: 15, fontWeight: 700, color: '#f1f5f9', fontFamily: 'monospace' }}>
              {formatTime(timeList[currentTimeIndex])}
            </div>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {loading && <div style={{ width: 8, height: 8, borderRadius: '50%', background: '#6366f1', boxShadow: '0 0 8px #6366f1', animation: 'pulse 1s infinite' }} />}
          {isLive && (
            <div style={{ background: 'rgba(239,68,68,0.15)', border: '1px solid rgba(239,68,68,0.4)', borderRadius: 6, padding: '3px 8px', fontSize: 10, fontWeight: 800, color: '#ef4444', letterSpacing: '0.1em' }}>● LIVE</div>
          )}
          <div style={{ fontSize: 11, color: '#334155', fontFamily: 'monospace' }}>
            {String(currentTimeIndex + 1).padStart(2, '0')} / {String(timeList.length).padStart(2, '0')}
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <span style={{ fontSize: 10, color: '#475569', whiteSpace: 'nowrap', minWidth: 34 }}>{formatShortDate(timeList[0])}</span>
        <div style={{ flex: 1, position: 'relative', height: 28, display: 'flex', alignItems: 'center' }}>
          <div style={{ position: 'absolute', left: 0, right: 0, height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.06)' }} />
          <div style={{ position: 'absolute', left: 0, width: `${pct}%`, height: 4, borderRadius: 2, background: 'linear-gradient(to right, #4f46e5, #818cf8)' }} />
          {/* Tick marks */}
          {timeList.map((_, i) => {
            if (i % tickStep !== 0) return null;
            return (
              <div key={i} style={{
                position: 'absolute', left: `${(i / Math.max(timeList.length - 1, 1)) * 100}%`,
                width: 1.5, height: 8,
                background: i <= currentTimeIndex ? '#6366f1' : 'rgba(255,255,255,0.12)',
                borderRadius: 1, transform: 'translateX(-0.75px)', top: -2,
                pointerEvents: 'none',
              }} />
            );
          })}
          <input
            type="range" min={0} max={timeList.length - 1} step={1} value={currentTimeIndex}
            onChange={e => { setIsPlaying(false); setCurrentTimeIndex(parseInt(e.target.value)); }}
            style={{ position: 'absolute', left: 0, right: 0, width: '100%', height: 28, opacity: 0, cursor: 'pointer', zIndex: 2 }}
          />
          <div style={{
            position: 'absolute', left: `calc(${pct}% - 9px)`,
            width: 18, height: 18, borderRadius: '50%',
            background: '#0f172a', border: '2.5px solid #818cf8',
            boxShadow: '0 0 0 3px rgba(99,102,241,0.3), 0 2px 8px rgba(0,0,0,0.5)',
            pointerEvents: 'none', transition: 'left 0.08s',
          }} />
        </div>
        <span style={{ fontSize: 10, color: '#475569', whiteSpace: 'nowrap', minWidth: 34, textAlign: 'right' }}>{formatShortDate(timeList[timeList.length - 1])}</span>
      </div>
      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }`}</style>
    </div>
  );
}

// ─── Main App ─────────────────────────────────────────────────────────────────

export default function App() {
  const [data, setData] = useState([]);
  const [loading, setLoading] = useState(true);
  const [weightTemp, setWeightTemp] = useState(0.45);
  const [weightHum, setWeightHum] = useState(0.50);
  const [hoverInfo, setHoverInfo] = useState(null);
  const [timeList, setTimeList] = useState([]);
  const [currentTimeIndex, setCurrentTimeIndex] = useState(0);
  const [viewState, setViewState] = useState(INITIAL_VIEW_STATE);

  useEffect(() => {
    axios.get('http://localhost:8000/api/times')
      .then(res => setTimeList(res.data.times))
      .catch(err => console.error('Lỗi lấy thời gian:', err));
  }, []);

  useEffect(() => {
    if (timeList.length === 0) return;
    setLoading(true);
    axios.get(`http://localhost:8000/api/weather-grid?time=${timeList[currentTimeIndex]}`)
      .then(res => setData(res.data))
      .catch(err => console.error('Lỗi lấy grid:', err))
      .finally(() => setLoading(false));
  }, [timeList, currentTimeIndex]);

  const handleHover = useCallback(info => setHoverInfo(info), []);

  // Tính cell size dựa trên zoom để ô grid vừa khít không bị gap, không overlap
  // Giả sử data là lưới 1°×1°, ta tính pixel size tương ứng
  const GRID_RESOLUTION_DEG = 1.0; // độ phân giải lưới (thay đổi nếu cần)

  const layers = [
    // ── Lớp 1: PolygonLayer hiển thị các ô grid đất liền ──
    new PolygonLayer({
      id: 'risk-grid',
      data,
      pickable: true,
      stroked: false,        // Không border để blend mượt với nhau
      filled: true,
      extruded: false,

      // Mỗi điểm (lon, lat) là tâm ô 1°×1°
      // half = 0.5° để 4 góc tạo thành ô vuông
      getPolygon: d => {
        const half = GRID_RESOLUTION_DEG / 2;
        const lon = parseFloat(d.lon);
        const lat = parseFloat(d.lat);
        return [
          [lon - half, lat - half],
          [lon + half, lat - half],
          [lon + half, lat + half],
          [lon - half, lat + half],
        ];
      },

      getFillColor: d => {
        const risk = Math.min(Math.max((d.nT * weightTemp) + (d.nH * weightHum), 0), 1);
        const [r, g, b] = turboColor(risk);
        // Opacity: vùng rủi ro thấp mờ hơn, cao thì đặc hơn → nhìn sạch hơn
        const alpha = Math.round(140 + risk * 100); // 140..240
        return [r, g, b, alpha];
      },

      updateTriggers: {
        getFillColor: [weightTemp, weightHum],
      },

      onHover: handleHover,
    }),
  ];

  return (
    <div style={{ width: '100vw', height: '100vh', position: 'relative', overflow: 'hidden', background: '#060d1a' }}>

      <DeckGL
        viewState={viewState}
        onViewStateChange={({ viewState: vs }) => setViewState(vs)}
        controller={{ scrollZoom: { speed: 0.5 }, touchRotate: true }}
        layers={layers}
        getCursor={({ isDragging }) => isDragging ? 'grabbing' : 'crosshair'}
      >
        <Map
          mapStyle="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"
          attributionControl={false}
        />
      </DeckGL>

      {/* Logo */}
      <div style={{
        position: 'absolute', top: 20, left: 20, zIndex: 10,
        background: 'rgba(8,14,28,0.88)', backdropFilter: 'blur(12px)',
        border: '1px solid rgba(255,255,255,0.07)', borderRadius: 12,
        padding: '10px 14px', display: 'flex', alignItems: 'center', gap: 10,
      }}>
        <div style={{ width: 32, height: 32, borderRadius: 8, background: 'linear-gradient(135deg, #1d4ed8, #0ea5e9)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 16 }}>🌏</div>
        <div>
          <div style={{ fontSize: 12, fontWeight: 700, color: '#f1f5f9', lineHeight: 1.2 }}>SEA Weather Risk</div>
          <div style={{ fontSize: 10, color: '#475569' }}>UNDP Climate Dashboard</div>
        </div>
      </div>

      <ControlPanel weightTemp={weightTemp} setWeightTemp={setWeightTemp} weightHum={weightHum} setWeightHum={setWeightHum} />

      {/* Legend */}
      <div style={{ position: 'absolute', bottom: 110, left: 20, zIndex: 10 }}>
        <RiskLegend />
      </div>

      <HoverTooltip info={hoverInfo} weightTemp={weightTemp} weightHum={weightHum} />

      <TimelineBar timeList={timeList} currentTimeIndex={currentTimeIndex} setCurrentTimeIndex={setCurrentTimeIndex} loading={loading} />

      {/* Loading overlay */}
      {loading && data.length === 0 && (
        <div style={{
          position: 'absolute', inset: 0, zIndex: 50,
          background: 'rgba(6,13,26,0.85)', backdropFilter: 'blur(8px)',
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 16,
        }}>
          <div style={{ width: 48, height: 48, borderRadius: '50%', border: '3px solid rgba(99,102,241,0.2)', borderTop: '3px solid #6366f1', animation: 'spin 0.8s linear infinite' }} />
          <div style={{ fontSize: 14, color: '#64748b', fontWeight: 500 }}>Đang tải dữ liệu khí hậu…</div>
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
        </div>
      )}
    </div>
  );
}