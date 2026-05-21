import React, { useState, useEffect, useCallback } from 'react';
import DeckGL from '@deck.gl/react';
import { GeoJsonLayer } from '@deck.gl/layers';
import Map from 'react-map-gl/maplibre';
import 'maplibre-gl/dist/maplibre-gl.css';
import axios from 'axios';

const API = 'http://localhost:8001';

// ─── View mặc định: Bangkok ───────────────────────────────────
const INITIAL_VIEW_STATE = {
  longitude: 100.55,
  latitude: 13.75,
  zoom: 10,
  pitch: 0,
  bearing: 0,
};

// ─── Màu theo alert level + nhiệt độ ─────────────────────────
function alertColor(temp, level, alpha = 200) {
  if (level === 'DANGER')  return [220, 38,  38,  alpha]; // đỏ
  if (temp >= 37)          return [249, 115, 22,  alpha]; // cam đậm
  if (temp >= 35)          return [234, 179, 8,   alpha]; // vàng
  return                          [251, 191, 36,  alpha]; // vàng nhạt (WARNING thấp)
}

// ─── Tooltip ──────────────────────────────────────────────────
function Tooltip({ info }) {
  if (!info?.object) return null;
  const p = info.object.properties;
  const levelColor = p.alert_level === 'DANGER' ? '#ef4444' : '#f59e0b';

  return (
    <div style={{
      position: 'absolute', left: info.x, top: info.y,
      transform: 'translate(-50%, -115%)',
      pointerEvents: 'none', zIndex: 100, minWidth: 210,
    }}>
      {/* Arrow */}
      <div style={{
        position: 'absolute', bottom: -6, left: '50%', transform: 'translateX(-50%)',
        width: 0, height: 0,
        borderLeft: '6px solid transparent', borderRight: '6px solid transparent',
        borderTop: '6px solid rgba(8,14,28,0.97)',
      }} />
      <div style={{
        background: 'rgba(8,14,28,0.97)', backdropFilter: 'blur(16px)',
        border: `1px solid ${levelColor}40`, borderRadius: 12,
        padding: '12px 14px', boxShadow: '0 20px 60px rgba(0,0,0,0.6)',
      }}>
        {/* District name */}
        <div style={{ fontSize: 13, fontWeight: 700, color: '#f1f5f9', marginBottom: 8 }}>
          📍 {p.district_name || p.gid_2}
        </div>

        {/* Temp + Level */}
        <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
          <div style={{
            flex: 1, background: 'rgba(251,191,36,0.1)',
            border: '1px solid rgba(251,191,36,0.25)', borderRadius: 8, padding: '7px 10px',
          }}>
            <div style={{ fontSize: 9, color: '#92400e', fontWeight: 700, letterSpacing: '0.06em', marginBottom: 3 }}>🌡 NHIỆT ĐỘ</div>
            <div style={{ fontSize: 20, fontWeight: 800, color: '#fbbf24', lineHeight: 1 }}>
              {p.temperature_2m}<span style={{ fontSize: 11, fontWeight: 500, marginLeft: 2 }}>°C</span>
            </div>
          </div>
          <div style={{
            flex: 1, background: levelColor + '15',
            border: `1px solid ${levelColor}35`, borderRadius: 8, padding: '7px 10px',
            display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center',
          }}>
            <div style={{ fontSize: 9, color: '#94a3b8', fontWeight: 700, letterSpacing: '0.06em', marginBottom: 4 }}>MỨC ĐỘ</div>
            <div style={{ fontSize: 12, fontWeight: 800, color: levelColor }}>{p.alert_level}</div>
          </div>
        </div>

        {/* Forecast date */}
        <div style={{ fontSize: 10, color: '#475569' }}>
          Dự báo: <span style={{ color: '#94a3b8', fontWeight: 600 }}>{p.forecast_date}</span>
          &nbsp;·&nbsp; Ngưỡng: <span style={{ color: '#94a3b8' }}>{p.threshold}°C</span>
        </div>
      </div>
    </div>
  );
}

// ─── Sidebar: danh sách alerts ────────────────────────────────
function AlertSidebar({ geojson, selectedDate, onSelectDate, forecastDates, meta }) {
  if (!geojson) return null;

  const features = geojson.features || [];
  const danger  = features.filter(f => f.properties.alert_level === 'DANGER');
  const warning = features.filter(f => f.properties.alert_level === 'WARNING');

  return (
    <div style={{
      position: 'absolute', top: 20, right: 20, zIndex: 10,
      width: 300, maxHeight: 'calc(100vh - 40px)',
      background: 'rgba(8,14,28,0.92)', backdropFilter: 'blur(20px)',
      border: '1px solid rgba(255,255,255,0.07)', borderRadius: 16,
      display: 'flex', flexDirection: 'column',
      boxShadow: '0 25px 80px rgba(0,0,0,0.6)',
      overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{ padding: '16px 18px 12px', borderBottom: '1px solid rgba(255,255,255,0.07)' }}>
        <div style={{ fontSize: 10, fontWeight: 700, color: '#475569', letterSpacing: '0.12em', textTransform: 'uppercase', marginBottom: 4 }}>
          UNDP · Heatwave Monitor
        </div>
        <div style={{ fontSize: 17, fontWeight: 700, color: '#f1f5f9' }}>
          🌡 Cảnh báo nắng nóng
        </div>
        {meta && (
          <div style={{ fontSize: 10, color: '#475569', marginTop: 4 }}>
            Run: {meta.run_date} &nbsp;·&nbsp; Max: <span style={{ color: '#fbbf24' }}>{meta.max_temp}°C</span>
          </div>
        )}
      </div>

      {/* Forecast date tabs */}
      {forecastDates.length > 0 && (
        <div style={{
          display: 'flex', gap: 6, padding: '10px 14px',
          borderBottom: '1px solid rgba(255,255,255,0.07)',
          overflowX: 'auto',
        }}>
          {forecastDates.map(d => (
            <button
              key={d}
              onClick={() => onSelectDate(d)}
              style={{
                flexShrink: 0,
                padding: '4px 10px', borderRadius: 8, fontSize: 11, fontWeight: 600,
                cursor: 'pointer', border: 'none', transition: 'all 0.2s',
                background: selectedDate === d ? 'rgba(239,68,68,0.2)' : 'rgba(255,255,255,0.05)',
                color: selectedDate === d ? '#f87171' : '#64748b',
                outline: selectedDate === d ? '1px solid rgba(239,68,68,0.4)' : '1px solid transparent',
              }}
            >
              {d.slice(5)} {/* MM-DD */}
            </button>
          ))}
        </div>
      )}

      {/* Stats row */}
      <div style={{ display: 'flex', gap: 0, borderBottom: '1px solid rgba(255,255,255,0.07)' }}>
        <div style={{ flex: 1, padding: '10px 14px', borderRight: '1px solid rgba(255,255,255,0.07)' }}>
          <div style={{ fontSize: 9, color: '#ef4444', fontWeight: 700, letterSpacing: '0.08em', marginBottom: 3 }}>DANGER</div>
          <div style={{ fontSize: 22, fontWeight: 800, color: '#ef4444', lineHeight: 1 }}>{danger.length}</div>
          <div style={{ fontSize: 9, color: '#475569', marginTop: 2 }}>quận ≥ 38°C</div>
        </div>
        <div style={{ flex: 1, padding: '10px 14px' }}>
          <div style={{ fontSize: 9, color: '#f59e0b', fontWeight: 700, letterSpacing: '0.08em', marginBottom: 3 }}>WARNING</div>
          <div style={{ fontSize: 22, fontWeight: 800, color: '#f59e0b', lineHeight: 1 }}>{warning.length}</div>
          <div style={{ fontSize: 9, color: '#475569', marginTop: 2 }}>quận 35–38°C</div>
        </div>
      </div>

      {/* District list */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '8px 0' }}>
        {features.length === 0 && (
          <div style={{ padding: '20px', textAlign: 'center', color: '#334155', fontSize: 12 }}>
            ✅ Không có cảnh báo nào
          </div>
        )}
        {features.map((f, i) => {
          const p = f.properties;
          const isD = p.alert_level === 'DANGER';
          const levelColor = isD ? '#ef4444' : '#f59e0b';
          return (
            <div key={i} style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '8px 16px',
              borderBottom: '1px solid rgba(255,255,255,0.04)',
              transition: 'background 0.15s',
            }}
              onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.04)'}
              onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: '#e2e8f0', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {p.district_name || p.gid_2}
                </div>
                <div style={{ fontSize: 10, color: '#475569', marginTop: 1 }}>{p.forecast_date}</div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <div style={{ fontSize: 14, fontWeight: 700, color: '#fbbf24' }}>
                  {p.temperature_2m}°
                </div>
                <div style={{
                  fontSize: 9, fontWeight: 700, padding: '2px 6px', borderRadius: 5,
                  background: levelColor + '20', color: levelColor,
                  border: `1px solid ${levelColor}40`,
                }}>
                  {p.alert_level}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Legend ───────────────────────────────────────────────────
function Legend() {
  const items = [
    { color: '#dc2626', label: 'DANGER ≥ 38°C' },
    { color: '#f97316', label: '37–38°C' },
    { color: '#eab308', label: '35–37°C' },
    { color: '#fbbf24', label: 'WARNING < 37°C' },
  ];
  return (
    <div style={{
      position: 'absolute', bottom: 24, left: 20, zIndex: 10,
      background: 'rgba(8,14,28,0.88)', backdropFilter: 'blur(12px)',
      border: '1px solid rgba(255,255,255,0.08)', borderRadius: 12,
      padding: '10px 14px', minWidth: 160,
    }}>
      <div style={{ fontSize: 10, fontWeight: 700, color: '#64748b', letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 8 }}>
        Mức cảnh báo
      </div>
      {items.map(({ color, label }) => (
        <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5 }}>
          <div style={{ width: 12, height: 12, borderRadius: 3, background: color, flexShrink: 0 }} />
          <span style={{ fontSize: 11, color: '#94a3b8' }}>{label}</span>
        </div>
      ))}
    </div>
  );
}

// ─── Main App ─────────────────────────────────────────────────
export default function App() {
  const [viewState, setViewState]       = useState(INITIAL_VIEW_STATE);
  const [hoverInfo, setHoverInfo]       = useState(null);

  // Heatwave state
  const [runDate, setRunDate]           = useState(null);
  const [forecastDates, setForecastDates] = useState([]);
  const [selectedDate, setSelectedDate] = useState(null);
  const [geojson, setGeojson]           = useState(null);
  const [meta, setMeta]                 = useState(null);
  const [loading, setLoading]           = useState(true);
  const [error, setError]               = useState(null);

  // 1. Lấy run_date mới nhất + danh sách forecast_dates
  useEffect(() => {
    axios.get(`${API}/api/heatwave/dates`)
      .then(res => {
        const { latest_run_date, dates } = res.data;
        if (!latest_run_date) { setLoading(false); return; }
        setRunDate(latest_run_date);

        // Lấy forecast_dates của run mới nhất
        const latestEntry = dates.find(d => d.run_date === latest_run_date);
        const fDates = latestEntry?.forecast_dates || [];
        setForecastDates(fDates);

        // Mặc định chọn ngày đầu tiên (hôm nay)
        if (fDates.length > 0) setSelectedDate(fDates[0]);
      })
      .catch(err => {
        console.error(err);
        setError('Không thể tải danh sách ngày alert.');
        setLoading(false);
      });
  }, []);

  // 2. Fetch GeoJSON alerts khi runDate + selectedDate thay đổi
  useEffect(() => {
    if (!runDate || !selectedDate) return;
    setLoading(true);
    setError(null);

    axios.get(`${API}/api/heatwave/alerts`, {
      params: { run_date: runDate, forecast_date: selectedDate },
    })
      .then(res => {
        setGeojson(res.data);
        setMeta(res.data.meta);
      })
      .catch(err => {
        if (err.response?.status === 404) {
          // Ngày này không có alert — reset về empty
          setGeojson({ type: 'FeatureCollection', features: [] });
          setMeta(null);
        } else {
          console.error(err);
          setError('Lỗi tải dữ liệu alert.');
        }
      })
      .finally(() => setLoading(false));
  }, [runDate, selectedDate]);

  const handleHover = useCallback(info => setHoverInfo(info), []);

  // GeoJsonLayer — polygon districts
  const layers = geojson ? [
    new GeoJsonLayer({
      id: 'heatwave-districts',
      data: geojson,
      pickable: true,
      stroked: true,
      filled: true,
      getFillColor: f => {
        const { temperature_2m, alert_level } = f.properties;
        return alertColor(parseFloat(temperature_2m), alert_level, 190);
      },
      getLineColor: f => {
        const { temperature_2m, alert_level } = f.properties;
        return alertColor(parseFloat(temperature_2m), alert_level, 255);
      },
      getLineWidth: 2,
      lineWidthMinPixels: 1,
      onHover: handleHover,
      updateTriggers: {
        getFillColor: [selectedDate],
        getLineColor: [selectedDate],
      },
    }),
  ] : [];

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
        <div style={{
          width: 32, height: 32, borderRadius: 8,
          background: 'linear-gradient(135deg, #dc2626, #f97316)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 16,
        }}>🌡</div>
        <div>
          <div style={{ fontSize: 12, fontWeight: 700, color: '#f1f5f9', lineHeight: 1.2 }}>Heatwave Alert</div>
          <div style={{ fontSize: 10, color: '#475569' }}>UNDP Climate Dashboard</div>
        </div>
      </div>

      {/* Sidebar */}
      <AlertSidebar
        geojson={geojson}
        selectedDate={selectedDate}
        onSelectDate={setSelectedDate}
        forecastDates={forecastDates}
        meta={meta}
      />

      <Legend />

      <Tooltip info={hoverInfo} />

      {/* Loading overlay */}
      {loading && (
        <div style={{
          position: 'absolute', inset: 0, zIndex: 50,
          background: 'rgba(6,13,26,0.75)', backdropFilter: 'blur(6px)',
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 16,
        }}>
          <div style={{
            width: 44, height: 44, borderRadius: '50%',
            border: '3px solid rgba(239,68,68,0.2)',
            borderTop: '3px solid #ef4444',
            animation: 'spin 0.8s linear infinite',
          }} />
          <div style={{ fontSize: 13, color: '#64748b', fontWeight: 500 }}>Đang tải dữ liệu cảnh báo…</div>
          <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
        </div>
      )}

      {/* Error */}
      {error && (
        <div style={{
          position: 'absolute', bottom: 24, left: '50%', transform: 'translateX(-50%)',
          zIndex: 20, background: 'rgba(239,68,68,0.15)', backdropFilter: 'blur(8px)',
          border: '1px solid rgba(239,68,68,0.4)', borderRadius: 10,
          padding: '10px 18px', fontSize: 12, color: '#fca5a5',
        }}>
          ⚠ {error}
        </div>
      )}
    </div>
  );
}