"""
Implementation công thức WBGT theo Liljegren et al. (2008)
=============================================================
Tính Tg (black globe), Tnwb (natural wet-bulb), kết hợp với Tdb (dry-bulb,
dùng thẳng từ data) để ra WBGT = 0.7*Tnwb + 0.2*Tg + 0.1*Tdb.

*** LƯU Ý VỀ ĐỘ TIN CẬY CỦA CÁC HẰNG SỐ ***
- Khung thuật toán chính (vòng lặp năng lượng cho Tg/Tnwb, hệ số 0.7/0.2/0.1)
  bám sát Liljegren et al. (2008).
- Các hàm vật lý phụ (viscosity, thermal_cond, diffusivity) dùng correlation
  chuẩn công khai (Sutherland's law, Hilpert correlation cho hình trụ) - về
  bản chất vật lý tương đương với code gốc, nhưng KHÔNG đảm bảo khớp byte-for-byte
  với hằng số kinetic-theory cụ thể trong wbgt.c gốc của Liljegren (Argonne).
- TRƯỚC KHI DÙNG SỐ LIỆU NÀY CHO BÁO CÁO CHÍNH THỨC: chạy chéo qua PyWBGT
  (https://github.com/QINQINKONG/PyWBGT, cùng tác giả với Kong & Huber 2022/2024
  đã trích dẫn trong báo cáo trước) trên cùng input để xác nhận sai lệch nằm
  trong ngưỡng chấp nhận được (báo cáo trước trích: lệch <1°C trong đa số trường hợp,
  có thể tới -2°C ở điều kiện khô-nóng gió yếu - đúng kiểu khí hậu Delhi).
"""

import math
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# HẰNG SỐ VẬT LÝ (theo Liljegren 2008, độc lập với nguồn dữ liệu)
# ---------------------------------------------------------------------------
STEFANB = 5.6696e-8        # Hằng số Stefan-Boltzmann, W/m^2/K^4
R_AIR = 287.05             # Hằng số khí riêng của không khí khô, J/(kg*K)
PR = 0.71                  # Số Prandtl của không khí (xấp xỉ hằng số trong dải nhiệt độ này)
RATIO = 1003.5 * 1.6058    # Cp_air (J/kg/K) * M_air/M_h2o - hệ số cho công thức wet-bulb depression
                            # (BUG ĐÃ SỬA: ban đầu quên nhân Cp, chỉ để 1.6058 -> làm vòng lặp
                            # phân kỳ thành complex number do thiếu 3 bậc độ lớn)

EMIS_WICK = 0.95
ALB_WICK = 0.4
D_WICK = 0.007             # đường kính bấc vải ướt, m
L_WICK = 0.0254            # chiều dài bấc vải ướt, m

EMIS_GLOBE = 0.95
ALB_GLOBE = 0.05
D_GLOBE = 0.0508           # đường kính quả cầu đen chuẩn, m (2 inch)

EMIS_SFC = 0.999
# Albedo bề mặt - bản gốc Liljegren validate ở Yuma (sa mạc, albedo cao ~0.45).
# Cho dự án đô thị/UNDP (Bangkok, Delhi), bề mặt thực tế (bê tông/cỏ/đất) khác hẳn
# sa mạc Arizona - NÊN HIỆU CHỈNH giá trị này theo land-cover thực tế của từng
# district trước khi dùng kết quả chính thức. Tạm dùng giá trị trung gian.
ALB_SFC = 0.25

MIN_SPEED = 0.5            # m/s, sàn tốc độ gió tối thiểu (tránh chia 0 / Re quá nhỏ)
MAX_ITER = 50
CONVERGENCE = 0.02         # ngưỡng hội tụ, Kelvin


# ---------------------------------------------------------------------------
# HÀM NHIỆT ĐỘNG LỰC HỌC CƠ BẢN
# ---------------------------------------------------------------------------
def esat(T_K, P_hpa):
    """Áp suất hơi bão hòa (hPa) - công thức Bolton (1980), có hiệu chỉnh áp suất."""
    Tc = T_K - 273.15
    es = 6.1121 * math.exp(17.502 * Tc / (240.97 + Tc))
    es = es * (1.0007 + 3.46e-6 * P_hpa)
    return es


def dew_point_K(e_hpa):
    """Điểm sương (K) từ áp suất hơi nước thực tế e (hPa) - nghịch đảo Bolton."""
    e_hpa = max(e_hpa, 1e-6)
    ln_term = math.log(e_hpa / 6.1121)
    Tc = 240.97 * ln_term / (17.502 - ln_term)
    return Tc + 273.15


def emis_atm(T_K, rh_pct):
    """Độ phát xạ khí quyển (clear-sky), công thức dạng Idso/Brutsaert - không
    cần field riêng từ Open-Meteo, chỉ cần Tair + RH."""
    e = (rh_pct / 100.0) * esat(T_K, 1010.0)
    e = max(e, 1e-6)
    return 0.575 * (e ** 0.143)


def air_density(T_K, P_hpa):
    return (P_hpa * 100.0) / (R_AIR * T_K)


def viscosity(T_K):
    """Độ nhớt động lực học không khí (kg/(m*s)) - Sutherland's law."""
    mu0, T0, C = 1.716e-5, 273.15, 110.4
    return mu0 * (T_K / T0) ** 1.5 * (T0 + C) / (T_K + C)


def thermal_cond(T_K):
    """Độ dẫn nhiệt không khí (W/(m*K)) - xấp xỉ tuyến tính chuẩn quanh 250-320K."""
    return 0.0241 * (T_K / 273.15) ** 0.9


def diffusivity(T_K, P_hpa):
    """Hệ số khuếch tán hơi nước trong không khí (m^2/s)."""
    P_atm = P_hpa / 1013.25
    return 2.11e-5 * (T_K / 273.15) ** 1.94 / P_atm


def evap(T_K):
    """Ẩn nhiệt hóa hơi của nước (J/kg)."""
    Tc = T_K - 273.15
    return (2500.8 - 2.36 * Tc) * 1000.0


# ---------------------------------------------------------------------------
# HỆ SỐ TRUYỀN NHIỆT ĐỐI LƯU
# ---------------------------------------------------------------------------
def h_sphere_in_air(diameter, T_K, P_hpa, speed):
    """Hệ số truyền nhiệt đối lưu cho hình cầu (Tg) - Whitaker correlation."""
    speed = max(speed, MIN_SPEED)
    rho = air_density(T_K, P_hpa)
    mu = viscosity(T_K)
    Re = speed * rho * diameter / mu
    Nu = 2.0 + 0.6 * (Re ** 0.5) * (PR ** (1.0 / 3.0))
    return Nu * thermal_cond(T_K) / diameter


def h_cylinder_in_air(diameter, length, T_K, P_hpa, speed):
    """Hệ số truyền nhiệt đối lưu cho hình trụ (Tnwb) - Hilpert correlation
    (C=0.683, m=0.466 cho dải Re 40-4000, đúng dải Re của wick ở tốc độ gió thực tế)."""
    speed = max(speed, MIN_SPEED)
    rho = air_density(T_K, P_hpa)
    mu = viscosity(T_K)
    Re = speed * rho * diameter / mu
    Nu = 0.683 * (Re ** 0.466) * (PR ** (1.0 / 3.0))
    return Nu * thermal_cond(T_K) / diameter


# ---------------------------------------------------------------------------
# Tg - BLACK GLOBE TEMPERATURE
# ---------------------------------------------------------------------------
def Tglobe(Tair_K, rh, Pair_hpa, speed, solar, fdir, cza):
    Tglobe_prev = Tair_K
    for _ in range(MAX_ITER):
        Tref = 0.5 * (Tglobe_prev + Tair_K)
        h = h_sphere_in_air(D_GLOBE, Tref, Pair_hpa, speed)
        cza_eff = max(cza, 0.001)  # tránh chia 0 khi mặt trời ở/dưới đường chân trời
        Tglobe_new = (
            0.5 * (emis_atm(Tair_K, rh) * Tair_K ** 4 + EMIS_SFC * Tair_K ** 4)
            - h / (STEFANB * EMIS_GLOBE) * (Tglobe_prev - Tair_K)
            + solar / (2 * STEFANB * EMIS_GLOBE) * (1 - ALB_GLOBE)
              * (fdir * (1 / (2 * cza_eff) - 1) + 1 + ALB_SFC)
        ) ** 0.25
        if abs(Tglobe_new - Tglobe_prev) < CONVERGENCE:
            Tglobe_prev = Tglobe_new
            break
        Tglobe_prev = 0.9 * Tglobe_prev + 0.1 * Tglobe_new
    return Tglobe_prev - 273.15  # Kelvin -> Celsius


# ---------------------------------------------------------------------------
# Tnwb - NATURAL WET-BULB TEMPERATURE
# ---------------------------------------------------------------------------
def Twb(Tair_K, rh, Pair_hpa, speed, solar, fdir, cza, rad=1):
    eair = (rh / 100.0) * esat(Tair_K, Pair_hpa)
    Twb_prev = dew_point_K(eair)  # giá trị đoán ban đầu = điểm sương
    sza = math.acos(max(min(cza, 1.0), -1.0))

    for _ in range(MAX_ITER):
        Tref = 0.5 * (Twb_prev + Tair_K)
        h = h_cylinder_in_air(D_WICK, L_WICK, Tref, Pair_hpa, speed)

        Fatm = STEFANB * EMIS_WICK * (
            0.5 * (emis_atm(Tair_K, rh) * Tair_K ** 4 + EMIS_SFC * Tair_K ** 4) - Twb_prev ** 4
        )
        if rad == 1:
            cza_eff = max(cza, 0.001)
            Fatm += (1 - ALB_WICK) * solar * (
                (1 - fdir) * (1 + 0.25 * D_WICK / L_WICK)
                + fdir * (math.tan(sza) / math.pi + 0.25 * D_WICK / L_WICK)
                + ALB_SFC
            )

        ewick = esat(Twb_prev, Pair_hpa)
        rho = air_density(Tref, Pair_hpa)
        Sc = viscosity(Tref) / (rho * diffusivity(Tref, Pair_hpa))

        Twb_new = (
            Tair_K
            - evap(Tref) / RATIO * (ewick - eair) / (Pair_hpa - ewick) * (PR / Sc) ** 0.56
            + (Fatm / h if h > 0 else 0)
        )
        if abs(Twb_new - Twb_prev) < CONVERGENCE:
            Twb_prev = Twb_new
            break
        Twb_prev = 0.9 * Twb_prev + 0.1 * Twb_new

    return Twb_prev - 273.15  # Kelvin -> Celsius


# ---------------------------------------------------------------------------
# GÓC THIÊN ĐỈNH MẶT TRỜI (cza) - thuật toán xấp xỉ NOAA Solar Position
# ---------------------------------------------------------------------------
def solar_cza(dt_utc, lat_deg, lon_deg):
    doy = dt_utc.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (doy - 1 + (dt_utc.hour - 12) / 24)

    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))

    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))

    time_offset = eqtime + 4 * lon_deg
    tst = dt_utc.hour * 60 + dt_utc.minute + time_offset
    ha_deg = (tst / 4) - 180
    ha_rad = math.radians(ha_deg)
    lat_rad = math.radians(lat_deg)

    cza = (math.sin(lat_rad) * math.sin(decl)
           + math.cos(lat_rad) * math.cos(decl) * math.cos(ha_rad))
    return cza


# ---------------------------------------------------------------------------
# HÀM TỔNG HỢP: TÍNH WBGT TỪ 1 DÒNG DỮ LIỆU OPEN-METEO
# ---------------------------------------------------------------------------
def compute_wbgt(local_time_str, utc_offset_hours, lat, lon,
                  tdb_c, rh_pct, wind_ms, shortwave, direct, diffuse, pressure_hpa):
    Tair_K = tdb_c + 273.15

    local_dt = datetime.fromisoformat(local_time_str)
    utc_dt = local_dt - timedelta(hours=utc_offset_hours)
    cza = solar_cza(utc_dt, lat, lon)

    if shortwave is None or shortwave < 1:
        fdir = 0.0
        solar = 0.0
    else:
        solar = shortwave
        fdir = direct / shortwave if shortwave > 0 else 0.0
        fdir = min(max(fdir, 0.0), 1.0)

    tg = Tglobe(Tair_K, rh_pct, pressure_hpa, wind_ms, solar, fdir, cza)
    tnwb = Twb(Tair_K, rh_pct, pressure_hpa, wind_ms, solar, fdir, cza, rad=1)
    twb_psychro = Twb(Tair_K, rh_pct, pressure_hpa, wind_ms, solar, fdir, cza, rad=0)

    wbgt = 0.7 * tnwb + 0.2 * tg + 0.1 * tdb_c

    return {
        "cza": round(cza, 3),
        "Tdb": round(tdb_c, 2),
        "Tnwb": round(tnwb, 2),
        "Tg": round(tg, 2),
        "Twb_psychrometric": round(twb_psychro, 2),
        "WBGT": round(wbgt, 2),
    }


# ---------------------------------------------------------------------------
# DỮ LIỆU MẪU THẬT - lấy nguyên từ console output của bạn (12 giờ đầu, mỗi nơi)
# ---------------------------------------------------------------------------
BANGKOK = dict(lat=13.743409, lon=100.495865, utc_offset=7, rows=[
    # time,                tdb,   rh,   wind, sw,    direct, diffuse, pressure
    ("2026-06-30T00:00", 27.90, 77.0, 1.48,   0.0,   0.0,    0.0,   1007.40),
    ("2026-06-30T01:00", 27.70, 77.0, 1.45,   0.0,   0.0,    0.0,   1006.80),
    ("2026-06-30T02:00", 27.30, 76.0, 1.58,   0.0,   0.0,    0.0,   1006.40),
    ("2026-06-30T03:00", 27.00, 74.0, 1.88,   0.0,   0.0,    0.0,   1006.00),
    ("2026-06-30T04:00", 27.00, 72.0, 2.33,   0.0,   0.0,    0.0,   1005.90),
    ("2026-06-30T05:00", 26.80, 75.0, 1.60,   0.0,   0.0,    0.0,   1006.00),
    ("2026-06-30T06:00", 26.60, 76.0, 1.50,   0.0,   0.0,    0.0,   1006.10),
    ("2026-06-30T07:00", 27.20, 73.0, 1.23,  50.0,   7.0,   43.0,   1006.70),
    ("2026-06-30T08:00", 28.90, 66.0, 1.80, 209.0,  74.0,  135.0,   1007.50),
    ("2026-06-30T09:00", 30.20, 62.0, 2.30, 361.0, 146.0,  215.0,   1007.80),
    ("2026-06-30T10:00", 31.40, 57.0, 2.71, 437.0, 125.0,  312.0,   1007.90),
    ("2026-06-30T11:00", 32.30, 56.0, 2.53, 561.0, 161.0,  400.0,   1007.60),
])

NEW_DELHI = dict(lat=28.576448, lon=77.18678, utc_offset=5.5, rows=[
    ("2026-06-30T00:00", 34.20, 50.0, 1.86,   0.0,   0.0,    0.0,    974.00),
    ("2026-06-30T01:00", 28.90, 78.0, 3.97,   0.0,   0.0,    0.0,    973.70),
    ("2026-06-30T02:00", 30.30, 67.0, 2.52,   0.0,   0.0,    0.0,    972.50),
    ("2026-06-30T03:00", 31.00, 66.0, 2.78,   0.0,   0.0,    0.0,    972.90),
    ("2026-06-30T04:00", 31.70, 60.0, 1.81,   0.0,   0.0,    0.0,    973.50),
    ("2026-06-30T05:00", 31.90, 59.0, 1.38,   0.0,   0.0,    0.0,    974.20),
    ("2026-06-30T06:00", 32.50, 57.0, 0.65,  27.0,   4.0,   23.0,    975.20),
    ("2026-06-30T07:00", 33.00, 55.0, 0.90, 139.0,  34.0,  105.0,    976.30),
    ("2026-06-30T08:00", 33.90, 52.0, 0.72, 270.0, 102.0,  168.0,    976.80),
    ("2026-06-30T09:00", 35.90, 42.0, 0.76, 517.0, 315.0,  202.0,    976.60),
    ("2026-06-30T10:00", 37.70, 36.0, 1.93, 660.0, 415.0,  245.0,    976.40),
    ("2026-06-30T11:00", 38.60, 34.0, 2.67, 667.0, 347.0,  320.0,    976.20),
])


def run_demo(name, loc):
    print("\n" + "=" * 100)
    print(f"{name}  (lat={loc['lat']}, lon={loc['lon']}, UTC{'+' if loc['utc_offset']>=0 else ''}{loc['utc_offset']})")
    print("=" * 100)
    header = f"{'Time':17s}{'Tdb':>7s}{'RH%':>6s}{'Wind':>7s}{'cza':>7s}{'Tnwb':>7s}{'Tg':>8s}{'Twb_psy':>9s}{'WBGT':>7s}  flags"
    print(header)

    for (t, tdb, rh, wind, sw, direct, diffuse, pres) in loc["rows"]:
        r = compute_wbgt(t, loc["utc_offset"], loc["lat"], loc["lon"],
                          tdb, rh, wind, sw, direct, diffuse, pres)

        flags = []
        if r["Tnwb"] > r["Tdb"] + 0.1:
            flags.append("Tnwb>Tdb")
        if sw > 50 and r["Tg"] <= r["Tdb"] + 0.5:
            flags.append("Tg khong tang du co nang")
        if r["Tnwb"] < r["Twb_psychrometric"] - 0.1:
            flags.append("Tnwb<Twb_psychrometric (bat thuong)")
        if r["WBGT"] > r["Tdb"] + 5:
            flags.append("WBGT vuot Tdb qua nhieu")

        flag_str = " | ".join(flags) if flags else "OK"
        print(f"{t:17s}{r['Tdb']:7.2f}{rh:6.0f}{wind:7.2f}{r['cza']:7.3f}"
              f"{r['Tnwb']:7.2f}{r['Tg']:8.2f}{r['Twb_psychrometric']:9.2f}{r['WBGT']:7.2f}  {flag_str}")


if __name__ == "__main__":
    run_demo("BANGKOK, THAILAND", BANGKOK)
    run_demo("NEW DELHI, INDIA", NEW_DELHI)