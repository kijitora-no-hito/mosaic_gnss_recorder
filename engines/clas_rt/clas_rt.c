/* clas_rt : CLASLIB による CLAS(PPP-RTK)のリアルタイム測位エンジン(Windows 用)
 *
 * 使い方: clas_rt -k 設定ファイル -p TCPポート -o 出力.pos [-a アドレス] [-x トレースレベル] [-l 遅延秒]
 * 試験用: clas_rt -k 設定ファイル -f 入力.sbf -o 出力.pos [-d 観測データの書き出し先] [-l 遅延秒]
 * 観測は既定で 5 秒待たせてから測位する(-l)。CLAS 補正は 5 秒のサブフレームで順に届くので、
 * 受けた直後に測位すると時刻の揃わない補正の組で解いてしまい、FIX の精度が大きく落ちる
 * (2026-11-03 走行データで FIX の水平誤差中央値 0.34 m → 遅延 4 秒以上で 0.10 m)。
 * recorder の TCP 配信(SBF)に接続し、観測データ(MeasEpoch)、航法メッセージ
 * (生ビットの GPSRawCA, QZSRawL1CA, GALRawINAV、または解読済みの GPSNav, QZSNav, GALNav)、
 * L6D(QZSRawL6D)を読み取って測位する。
 * CLAS 補正の更新手順は CLASLIB の後処理(postpos.c の inputobs / procpos)と同じ。
 * 1 秒ごとに状態を標準出力へ 1 行出す。Ctrl+C / Ctrl+Break で止める。
 * 状態行: STAT 日付 時刻(GPST) Q 衛星数 緯度[deg] 経度[deg] 楕円体高[m] 経過秒 入力バイト数 L6D衛星
 * 解が無いときは Q=0 で緯度経度高さは 0。
 *
 * SBF の読み取りは MALIB の rcv/septentrio.c(BSD 2 条項)を CLASLIB の型に合わせて移植した。
 * GPSNav / QZSNav / GALNav の読み取りは新規(項目配置は sbf2rin の RINEX 航法と照合済み)。
 * 観測コードは CLASLIB の後処理で使うものに読み替える(GPS/QZS L2C→2X, L5→5X, GAL E1→1X, E5a→5X)。
 * CLASLIB(BSD 2 条項 + 追加条項)と同じライセンス条件に従う。
 */
#include <signal.h>
#include <winsock2.h>
#include "postpos.c"

#define SBF_MEASEPOCH   4027
#define SBF_GPSRAWCA    4017
#define SBF_GALRAWINAV  4023
#define SBF_QZSRAWL1CA  4066
#define SBF_QZSRAWL6D   4270
#define SBF_GPSNAV      5891
#define SBF_QZSNAV      4095
#define SBF_GALNAV      4002
#define L6_FRAME_LEN    250
#define L6_QUEUE_LEN    (L6_FRAME_LEN * 64)
#define L6_SWITCH_SEC   30.0

#define U1(p) (*((uint8_t *)(p)))
#define I1(p) (*((int8_t  *)(p)))
static uint16_t U2(const uint8_t *p) {uint16_t a; memcpy(&a, p, 2); return a;}
static uint32_t U4(const uint8_t *p) {uint32_t a; memcpy(&a, p, 4); return a;}
static int32_t  I4(const uint8_t *p) {int32_t  a; memcpy(&a, p, 4); return a;}

typedef struct {        /* SBF 読み取りの状態 */
    uint8_t buff[MAXRAWLEN];
    int nbyte, len;
    gtime_t time;
    obsd_t obs[MAXOBS];
    int nobs;
    double lockt[MAXSAT][NFREQ];
    uint8_t subfrm[MAXSAT][380];
} sbf_t;

typedef struct {        /* L6D の受信待ち行列(1 衛星分) */
    uint8_t data[L6_QUEUE_LEN];
    int head, n;
    int svid;           /* 使っている衛星の SBF SVID(0:未選択) */
    gtime_t last;       /* その衛星から最後に受けた時刻 */
    int frames;
    int skip;           /* 欠けの後、次のサブフレーム先頭まで捨てている */
    int dropped;        /* 欠けのために捨てたフレーム数 */
} l6q_t;

static volatile int stopflag = 0;
static sbf_t sbf;
static l6q_t l6q;
static rtk_t rtk;
static int l6start = 0;
static FILE *fpdump = NULL; /* -d: 測位に渡す観測データの書き出し(照合用) */

extern int showmsg(char *format, ...) { return 0; }
extern void settspan(gtime_t ts, gtime_t te) {}
extern void settime(gtime_t time) {}

static void onsignal(int sig)
{
    stopflag = 1;
    signal(sig, onsignal);
}

/* SBF SVID → 衛星番号(GPS / Galileo / QZSS だけ使う) */
static int svid2sat(int svid)
{
    if (1 <= svid && svid <= 37) return satno(SYS_GPS, svid);
    if (71 <= svid && svid <= 106) return satno(SYS_GAL, svid - 70);
    if (181 <= svid && svid <= 187) return satno(SYS_QZS, svid - 180 + MINPRNQZS - 1);
    return 0;
}

/* SBF の信号番号 → 周波数インデックスと観測コード。CLAS で使わない信号は -1 */
static int sig2idx(int sys, int sig, uint8_t *code, double *freq)
{
    switch (sys) {
    case SYS_GPS:
        if (sig == 0) { *code = CODE_L1C; *freq = FREQ1; return 0; }
        if (sig == 3) { *code = CODE_L2X; *freq = FREQ2; return 1; }
        if (sig == 4) { *code = CODE_L5X; *freq = FREQ5; return 2; }
        break;
    case SYS_QZS:
        if (sig == 6) { *code = CODE_L1C; *freq = FREQ1; return 0; }
        if (sig == 7) { *code = CODE_L2X; *freq = FREQ2; return 1; }
        if (sig == 26) { *code = CODE_L5X; *freq = FREQ5; return 2; }
        break;
    case SYS_GAL:
        /* E5b は入れない。CLASLIB はインデックス 1 の波長を L2 として扱うので、入れると
         * 幾何フリー結合が毎エポック跳んでサイクルスリップ扱いになる(RINEX の読み込みでも使われない) */
        if (sig == 17) { *code = CODE_L1X; *freq = FREQ1; return 0; }
        if (sig == 20) { *code = CODE_L5X; *freq = FREQ5; return 2; }
        break;
    }
    return -1;
}

static int cmpobs_sat(const void *p1, const void *p2)
{
    return (int)((const obsd_t *)p1)->sat - (int)((const obsd_t *)p2)->sat;
}

/* MeasEpoch(MALIB decode_measepoch を移植。メインアンテナだけ使う) */
static int decode_measepoch(sbf_t *s)
{
    uint8_t *p = s->buff + 14, *end = s->buff + s->len, code;
    double P1, P2, L1, L2, D1, D2, S, freq1, freq2;
    int i, j, k, idx, n, n1, len1, len2, n2, sig, ant, svid, info, sat, sys, lock, LLI;
    obsd_t *d;

    if (s->len < 20 || (U1(p + 3) & 0x80)) return -1; /* 短い、または観測量のスクランブル */
    n1 = U1(p);
    len1 = U1(p + 1);
    len2 = U1(p + 2);
    for (i = n = 0, p += 6; i < n1 && n < MAXOBS && p + 20 <= end; i++) {
        svid = U1(p + 2);
        ant = U1(p + 1) >> 5;
        sig = U1(p + 1) & 0x1f;
        info = U1(p + 18);
        n2 = U1(p + 19);
        if (sig == 31) sig = (info >> 3) + 32;
        sat = svid2sat(svid);
        sys = sat ? satsys(sat, NULL) : 0;
        if (ant != 0 || !sat || (idx = sig2idx(sys, sig, &code, &freq1)) < 0) {
            p += len1 + len2 * n2;
            continue;
        }
        d = s->obs + n;
        memset(d, 0, sizeof(*d));
        d->time = s->time;
        d->sat = (unsigned char)sat;
        d->rcv = 1;
        P1 = D1 = 0.0;
        if ((U1(p + 3) & 0x1f) != 0 || U4(p + 4) != 0) {
            P1 = (U1(p + 3) & 0x0f) * 4294967.296 + U4(p + 4) * 0.001;
            d->P[idx] = P1;
        }
        if (I4(p + 8) != (-2147483647 - 1)) {
            D1 = I4(p + 8) * 0.0001;
            d->D[idx] = (float)D1;
        }
        lock = U2(p + 16);
        /* 半波長の不定が解けていない位相(ObsInfo bit2)は使わない(sbf2rin も RINEX に出さない) */
        if (P1 != 0.0 && lock != 65535 && !(info & (1 << 2)) &&
            (I1(p + 14) != -128 || U2(p + 12) != 0)) {
            L1 = I1(p + 14) * 65.536 + U2(p + 12) * 0.001;
            d->L[idx] = P1 * freq1 / CLIGHT + L1;
            LLI = (lock < s->lockt[sat - 1][idx] ? 1 : 0) + ((info & (1 << 2)) ? 2 : 0);
            d->LLI[idx] = (unsigned char)LLI;
            s->lockt[sat - 1][idx] = lock;
        }
        if (U1(p + 15) != 255) {
            S = U1(p + 15) * 0.25 + 10.0;
            d->SNR[idx] = (unsigned char)(S * 4.0 + 0.5);
        }
        d->code[idx] = code;

        for (j = 0, p += len1; j < n2 && p + 12 <= end; j++, p += len2) {
            sig = U1(p) & 0x1f;
            ant = U1(p) >> 5;
            info = U1(p + 5);
            if (sig == 31) sig = (info >> 3) + 32;
            if (ant != 0 || (k = sig2idx(sys, sig, &code, &freq2)) < 0) continue;
            P2 = 0.0;
            if (P1 != 0.0 && (getbits(p + 3, 5, 3) != -4 || U2(p + 6) != 0)) {
                P2 = P1 + getbits(p + 3, 5, 3) * 65.536 + U2(p + 6) * 0.001;
                d->P[k] = P2;
            }
            if (P2 != 0.0 && !(info & (1 << 2)) && (I1(p + 4) != -128 || U2(p + 8) != 0)) {
                L2 = I1(p + 4) * 65.536 + U2(p + 8) * 0.001;
                d->L[k] = P2 * freq2 / CLIGHT + L2;
            }
            if (D1 != 0.0 && (getbits(p + 3, 0, 5) != -16 || U2(p + 10) != 0)) {
                D2 = getbits(p + 3, 0, 5) * 6.5536 + U2(p + 10) * 0.0001;
                d->D[k] = (float)(D1 * freq2 / freq1) + (float)D2;
            }
            lock = U1(p + 1);
            if (lock != 255) {
                LLI = (lock < s->lockt[sat - 1][k] ? 1 : 0) + ((info & (1 << 2)) ? 2 : 0);
                d->LLI[k] = (unsigned char)LLI;
                s->lockt[sat - 1][k] = lock;
            }
            if (U1(p + 2) != 255) {
                S = U1(p + 2) * 0.25 + 10.0;
                d->SNR[k] = (unsigned char)(S * 4.0 + 0.5);
            }
            d->code[k] = code;
        }
        n++;
    }
    qsort(s->obs, n, sizeof(obsd_t), cmpobs_sat);
    s->nobs = n;
    return 1;
}

/* 航法メッセージを rtcm.nav に入れる。[sat-1] が最新、[sat-1+MAXSAT] が 1 つ前 */
static int store_eph(const eph_t *eph)
{
    eph_t *cur = rtcm.nav.eph + eph->sat - 1;

    if (cur->sat == eph->sat && cur->iode == eph->iode &&
        timediff(cur->toe, eph->toe) == 0.0) return 0;
    if (cur->sat == eph->sat) rtcm.nav.eph[eph->sat - 1 + MAXSAT] = *cur;
    *cur = *eph;
    return 2;
}

/* GPSRawCA / QZSRawL1CA(MALIB decode_rawca を移植) */
static int decode_rawca(sbf_t *s, int sys)
{
    uint8_t *p = s->buff + 14, buff[30];
    eph_t eph = {0};
    int i, sat, id;

    if (s->len < 60) return -1;
    if (!(sat = svid2sat(U1(p))) || satsys(sat, NULL) != sys) return -1;
    if (!U1(p + 1)) return 0; /* パリティ不良 */
    for (i = 0, p += 6; i < 10; i++, p += 4) setbitu(buff, 24 * i, 24, U4(p) >> 6);
    id = getbitu(buff, 43, 3);
    if (id < 1 || id > 5) return -1;
    memcpy(s->subfrm[sat - 1] + (id - 1) * 30, buff, 30);
    if (id != 3) return 0;
    if (decode_frame(s->subfrm[sat - 1], &eph, NULL, NULL, NULL, NULL) != 1 ||
        decode_frame(s->subfrm[sat - 1] + 30, &eph, NULL, NULL, NULL, NULL) != 2 ||
        decode_frame(s->subfrm[sat - 1] + 60, &eph, NULL, NULL, NULL, NULL) != 3) return 0;
    if (eph.iode != (eph.iodc & 0xff)) return 0; /* サブフレームの組が揃っていない */
    eph.sat = sat;
    return store_eph(&eph);
}

/* GALRawINAV(MALIB decode_galrawinav を移植) */
static int decode_galrawinav(sbf_t *s)
{
    uint8_t *p = s->buff + 14, buff[32];
    eph_t eph = {0};
    int i, j, sat, src, type;

    if (s->len < 52) return -1;
    if (!(sat = svid2sat(U1(p))) || satsys(sat, NULL) != SYS_GAL) return -1;
    src = U1(p + 3) & 0x1f;
    if (src != 17 && src != 21 && src != 22) return -1;
    if (!U1(p + 1)) return 0; /* CRC 不良 */
    for (i = 0; i < 8; i++) setbitu(buff, 32 * i, 32, U4(p + 6 + i * 4));
    if (getbitu(buff, 0, 1) != 0 || getbitu(buff, 114, 1) != 1) return -1; /* even/odd */
    if (getbitu(buff, 1, 1) || getbitu(buff, 115, 1)) return 0; /* alert page */
    type = getbitu(buff, 2, 6);
    if (type > 6) return 0;
    for (i = 0, j = 2; i < 14; i++, j += 8) s->subfrm[sat - 1][type * 16 + i] = getbitu(buff, j, 8);
    for (i = 14, j = 116; i < 16; i++, j += 8) s->subfrm[sat - 1][type * 16 + i] = getbitu(buff, j, 8);
    if (type != 5) return 0;
    if (!decode_gal_inav(s->subfrm[sat - 1], &eph) || eph.sat != sat) return 0;
    return store_eph(&eph);
}

/* 1024 週の不定を、ブロックの時刻に最も近い週で解く */
static int fullweek(int wn, gtime_t time)
{
    int week;

    time2gpst(time, &week);
    return wn + (int)floor((week - wn + 512) / 1024.0) * 1024;
}

static double F4(const uint8_t *p) {float a; memcpy(&a, p, 4); return a;}
static double F8(const uint8_t *p) {double a; memcpy(&a, p, 8); return a;}

/* GPSNav / QZSNav(受信機が解読済みの航法メッセージ。生ビットが無い記録でも使えるように) */
static int decode_gpsnav(sbf_t *s, int sys)
{
    uint8_t *p = s->buff + 14;
    eph_t eph = {0};
    int prn = U1(p), sat, week;

    if (s->len < 140) return -1;
    sat = (sys == SYS_GPS) ? satno(SYS_GPS, prn) : satno(SYS_QZS, prn - 180 + MINPRNQZS - 1);
    if (!sat) return -1;
    eph.sat = sat;
    eph.code = U1(p + 4);
    eph.sva = U1(p + 5);
    eph.svh = U1(p + 6);
    eph.flag = U1(p + 7);
    eph.iodc = U2(p + 8);
    eph.iode = U1(p + 10);
    if (eph.iode != U1(p + 11)) return 0; /* IODE2 と IODE3 が不一致 */
    eph.fit = U1(p + 12) ? 0.0 : 4.0;
    eph.tgd[0] = F4(p + 14);
    eph.f2 = F4(p + 22);
    eph.f1 = F4(p + 26);
    eph.f0 = F4(p + 30);
    eph.crs = F4(p + 34);
    eph.deln = F4(p + 38) * PI;
    eph.M0 = F8(p + 42) * PI;
    eph.cuc = F4(p + 50);
    eph.e = F8(p + 54);
    eph.cus = F4(p + 62);
    eph.A = F8(p + 66) * F8(p + 66);
    eph.toes = U4(p + 74);
    eph.cic = F4(p + 78);
    eph.OMG0 = F8(p + 82) * PI;
    eph.cis = F4(p + 90);
    eph.i0 = F8(p + 94) * PI;
    eph.crc = F4(p + 102);
    eph.omg = F8(p + 106) * PI;
    eph.OMGd = F4(p + 114) * PI;
    eph.idot = F4(p + 118) * PI;
    eph.toc = gpst2time(fullweek(U2(p + 122), s->time), U4(p + 18));
    eph.week = week = fullweek(U2(p + 124), s->time);
    eph.toe = gpst2time(week, eph.toes);
    eph.ttr = s->time;
    return store_eph(&eph);
}

/* GALNav(I/NAV 由来のものだけ使う。WNt_oe は GPS 週と同じ数え方) */
static int decode_galnav(sbf_t *s)
{
    uint8_t *p = s->buff + 14;
    eph_t eph = {0};
    int sat, health;

    if (s->len < 149) return -1;
    if (!(sat = svid2sat(U1(p))) || satsys(sat, NULL) != SYS_GAL) return -1;
    if (U1(p + 1) != 2) return 0; /* Source 2: I/NAV */
    eph.sat = sat;
    eph.A = F8(p + 2) * F8(p + 2);
    eph.M0 = F8(p + 10) * PI;
    eph.e = F8(p + 18);
    eph.i0 = F8(p + 26) * PI;
    eph.omg = F8(p + 34) * PI;
    eph.OMG0 = F8(p + 42) * PI;
    eph.OMGd = F4(p + 50) * PI;
    eph.idot = F4(p + 54) * PI;
    eph.deln = F4(p + 58) * PI;
    eph.cuc = F4(p + 62);
    eph.cus = F4(p + 66);
    eph.crc = F4(p + 70);
    eph.crs = F4(p + 74);
    eph.cic = F4(p + 78);
    eph.cis = F4(p + 82);
    eph.toes = U4(p + 86);
    eph.f2 = F4(p + 94);
    eph.f1 = F4(p + 98);
    eph.f0 = F8(p + 102);
    eph.week = U2(p + 110);
    eph.toe = gpst2time(eph.week, eph.toes);
    eph.toc = gpst2time(U2(p + 112), U4(p + 90));
    eph.iode = eph.iodc = U2(p + 114);
    /* Health_OSSOL: bit0-3 E1B(有効,HS×2,DVS), bit4-7 E5b → RTKLIB の svh の並びに直す */
    health = U2(p + 116);
    eph.svh = (((health >> 5) & 3) << 7) | (((health >> 7) & 1) << 6) |
              (((health >> 1) & 3) << 1) | ((health >> 3) & 1);
    eph.sva = U1(p + 120);
    eph.tgd[0] = F4(p + 122);
    eph.tgd[1] = F4(p + 126);
    eph.code = 1; /* I/NAV E1B */
    eph.ttr = s->time;
    return store_eph(&eph);
}

/* QZSRawL6D: 使う衛星を 1 つ選び、その 250 バイトを待ち行列に入れる */
static int decode_qzsrawl6d(sbf_t *s)
{
    uint8_t *p = s->buff + 14, frame[252];
    int i, svid, tail;

    if (s->len < 272) return -1;
    svid = U1(p);
    if (!U1(p + 1)) return 0; /* RS 復号失敗 */
    if (l6q.svid != svid) {
        if (l6q.svid && timediff(s->time, l6q.last) < L6_SWITCH_SEC) return 0;
        l6q.svid = svid; /* 最初の衛星、または今の衛星が途絶えたら切り替える */
        l6q.n = 0;
        l6q.skip = 1;
        rtcm_[0].nbyte = 0;
    }
    /* L6 は衛星ごとに 1 秒 1 フレーム。間が空いたら(RS 失敗・未受信)途中のフレームが欠けている。
     * CSSR はサブフレーム(5 フレーム)を連結して読むので、欠けたまま続きを渡すとビット列がずれて
     * でたらめな補正を読む。次のサブフレーム先頭(メッセージ種別の最下位ビット)まで捨てる */
    if (l6q.last.time && timediff(s->time, l6q.last) > 1.5) l6q.skip = 1;
    l6q.last = s->time;
    for (i = 0; i < 63; i++) {
        uint32_t w = U4(p + 6 + i * 4);
        frame[i * 4] = (uint8_t)(w >> 24);
        frame[i * 4 + 1] = (uint8_t)(w >> 16);
        frame[i * 4 + 2] = (uint8_t)(w >> 8);
        frame[i * 4 + 3] = (uint8_t)w;
    }
    if (l6q.skip) {
        if (!(frame[5] & 0x01)) {
            l6q.dropped++;
            return 0;
        }
        l6q.skip = 0;
    }
    for (i = 0; i < L6_FRAME_LEN; i++) {
        if (l6q.n >= L6_QUEUE_LEN) { /* 溢れたら古いものを捨てる */
            l6q.head = (l6q.head + 1) % L6_QUEUE_LEN;
            l6q.n--;
        }
        tail = (l6q.head + l6q.n) % L6_QUEUE_LEN;
        l6q.data[tail] = frame[i];
        l6q.n++;
    }
    l6q.frames++;
    return 0;
}

static int decode_sbf(sbf_t *s)
{
    uint8_t *p = s->buff;
    uint32_t tow, week;
    int type = U2(p + 4) & 0x1fff;

    if (rtk_crc16(p + 4, s->len - 4) != U2(p + 2) || s->len < 14) return -1;
    tow = U4(p + 8);
    week = U2(p + 12);
    if (tow == 4294967295u || week == 65535u) return -1;
    s->time = gpst2time(week, tow * 0.001);
    switch (type) {
    case SBF_MEASEPOCH:  return decode_measepoch(s);
    case SBF_GPSRAWCA:   return decode_rawca(s, SYS_GPS);
    case SBF_QZSRAWL1CA: return decode_rawca(s, SYS_QZS);
    case SBF_GALRAWINAV: return decode_galrawinav(s);
    case SBF_QZSRAWL6D:  return decode_qzsrawl6d(s);
    case SBF_GPSNAV:     return decode_gpsnav(s, SYS_GPS);
    case SBF_QZSNAV:     return decode_gpsnav(s, SYS_QZS);
    case SBF_GALNAV:     return decode_galnav(s);
    }
    return 0;
}

/* 1 バイトずつ SBF を組み立てる。戻り値は decode_sbf と同じ(1:観測データ) */
static int input_sbf(sbf_t *s, uint8_t data)
{
    if (s->nbyte == 0) {
        s->buff[0] = s->buff[1];
        s->buff[1] = data;
        if (s->buff[0] == 0x24 && s->buff[1] == 0x40) s->nbyte = 2;
        return 0;
    }
    s->buff[s->nbyte++] = data;
    if (s->nbyte < 8) return 0;
    s->len = U2(s->buff + 6);
    if (s->len <= 8 || s->len > MAXRAWLEN || (s->len & 3)) {
        s->nbyte = 0;
        return -1;
    }
    if (s->nbyte < s->len) return 0;
    s->nbyte = 0;
    return decode_sbf(s);
}

/* 待ち行列から CSSR を読む(cssr_misc.c の input_cssrf と同じ手順。無くなったら -2) */
static int input_cssr_queue(rtcm_t *cssr)
{
    static uint8_t frame = 0;
    static FILE *dummy_fp[CSSR_TYPE_NUM] = {NULL}; /* CSSR の解析出力は使わない(postpos と同じ) */
    uint8_t data;
    int i, ret;

    for (i = 0; i < 4096; i++) {
        if ((ret = decode_qzs_msg(cssr, 0, &frame, dummy_fp))) return ret;
        if (l6q.n <= 0) return -2;
        data = l6q.data[l6q.head];
        l6q.head = (l6q.head + 1) % L6_QUEUE_LEN;
        l6q.n--;
        if ((ret = input_cssr(cssr, data, &frame, dummy_fp))) return ret;
    }
    return 0;
}

/* 観測の遅延処理(-l 秒): 観測を delay 秒待たせ、その間に届いた L6 も使って測位する。
 * 0 なら受けたらすぐ測位する。後処理(postpos)が L6 を観測時刻より先まで読むのと同じ効果 */
#define DEFAULT_DELAY 5.0
#define DELAY_EPOCHS 128
static obsd_t delay_obs[DELAY_EPOCHS][MAXOBS];
static int delay_n[DELAY_EPOCHS], delay_head = 0, delay_count = 0;
static void procepoch(FILE *fp, prcopt_t *popt, const solopt_t *sopt, obsd_t *obs, int nobs);

static void procdelayed(FILE *fp, prcopt_t *popt, const solopt_t *sopt, double delay, int flush)
{
    int k;

    if (!flush) {
        if (delay <= 0.0) {
            procepoch(fp, popt, sopt, sbf.obs, sbf.nobs);
            return;
        }
        if (delay_count >= DELAY_EPOCHS) { /* 溢れたら古いものから測位する */
            procepoch(fp, popt, sopt, delay_obs[delay_head], delay_n[delay_head]);
            delay_head = (delay_head + 1) % DELAY_EPOCHS;
            delay_count--;
        }
        k = (delay_head + delay_count) % DELAY_EPOCHS;
        memcpy(delay_obs[k], sbf.obs, sizeof(obsd_t) * sbf.nobs);
        delay_n[k] = sbf.nobs;
        delay_count++;
    }
    while (delay_count > 0 && (flush || delay_n[delay_head] <= 0 ||
           timediff(sbf.time, delay_obs[delay_head][0].time) >= delay)) {
        procepoch(fp, popt, sopt, delay_obs[delay_head], delay_n[delay_head]);
        delay_head = (delay_head + 1) % DELAY_EPOCHS;
        delay_count--;
    }
}

/* 1 エポックの測位(postpos.c の inputobs の CSSR 部分と procpos のループ 1 回分) */
static void procepoch(FILE *fp, prcopt_t *popt, const solopt_t *sopt, obsd_t *obs, int nobs)
{
    int i, j, n, week;

    if (nobs <= 0) return;
    set_cur_tow(time2gpst(obs[0].time, NULL));
    if (!l6start) { /* postpos.c で L6D ファイルを開いたときの初期化 */
        rtcm.nav.rtcmmode = rtcm_[0].nav.rtcmmode = RTCMMODE_CSSR;
        rtcm_[0].time = obs[0].time;
        init_cssr_object(0);
        init_cntinuty_rslt(0);
        init_nondisp_buf(0);
        init_cssr_l6e_object(0);
        init_cssr_auth(0);
        init_null_msg_time(0);
        init_cssr_fac_rbuf(0);
        time2gpst(obs[0].time, &week);
        for (j = 0; j < (int)(sizeof(rtcm_[0].week_ref) / sizeof(int)); j++) {
            rtcm_[0].obs_ref[j] = obs[0].time;
            rtcm_[0].week_ref[j] = week;
        }
        init_fastfix_flag();
        rtcm.mode = RTCMMODE_CSSR;
        l6start = 1;
    }
    set_cssr_ch_idx(0);
    for (;;) {
        if (rtcm_[0].subtype == 1 && timediff(rtcm_[0].time, obs[0].time) > 0.0) break;
        if (input_cssr_queue(&rtcm_[0]) < -1) break;
    }
    updateclas(&rtcm, popt, obs[0].time, RTCMMODE_CSSR);
    clear_st13_1_output_queue();
    init_st13_3_last();
    init_verif_cssr_last();
    for (i = 0; i < RTCM_FILENUM; i++) init_cntinuty_rslt_out(i);

    for (i = n = 0; i < nobs; i++) {
        if ((satsys(obs[i].sat, NULL) & popt->navsys) && popt->exsats[obs[i].sat - 1] != 1) {
            obs[n++] = obs[i];
        }
    }
    if (n <= 0) return;
    if (fpdump) {
        for (i = 0; i < n; i++) {
            char id[8];
            satno2id(obs[i].sat, id);
            fprintf(fpdump, "%.1f %s", time2gpst(obs[i].time, NULL), id);
            for (j = 0; j < NFREQ; j++) {
                fprintf(fpdump, " %d %.3f %.3f %d %d", obs[i].code[j], obs[i].P[j], obs[i].L[j],
                        obs[i].LLI[j], obs[i].SNR[j]);
            }
            fprintf(fpdump, "\n");
        }
    }
    if (rtcm_[0].nav.filreset == TRUE) {
        rtcm.nav.filreset = rtcm_[0].nav.filreset;
        rtcm_[0].nav.filreset = FALSE;
    }
    if (!rtkpos(&rtk, obs, n, &rtcm.nav)) return;
    outsol(fp, &rtk.sol, rtk.rb, sopt);
    fflush(fp);
}

/* 後処理(postpos / execses)と同じ準備。観測ファイルの代わりに受信データを使う */
static int setup(prcopt_t *popt, solopt_t *sopt, filopt_t *fopt, const char *outfile)
{
    const eph_t eph0 = {0, -1, -1};
    gtime_t now = timeget();
    int i, j;

    if (fopt->grid[0] && read_grid_def(fopt->grid) != 0) {
        fprintf(stderr, "grid file error: %s\n", fopt->grid);
        return 0;
    }
    if (fopt->pubkeydir[0] && clas_load_pubkeys_from_dir(fopt->pubkeydir) == 0) {
        fprintf(stderr, "public key file error: %s\n", fopt->pubkeydir);
        return 0;
    }
    if (!openses(popt, sopt, fopt, &rtcm.nav, &pcvss, &pcvsr)) {
        fprintf(stderr, "open session error\n");
        return 0;
    }
    init_rtcm(&rtcm);
    init_st13_1_last();
    if (!(rtcm.nav.eph = (eph_t *)malloc(sizeof(eph_t) * MAXSAT * 2))) return 0;
    for (i = 0; i < MAXSAT * 2; i++) rtcm.nav.eph[i] = eph0;
    rtcm.nav.n = rtcm.nav.nmax = MAXSAT * 2;
    for (i = 0; i < MAXSAT; i++) for (j = 0; j < NFREQ; j++) {
        rtcm.nav.lam[i][j] = satwavelen(i + 1, j, &rtcm.nav);
    }
    setpcv(now, popt, &rtcm.nav, &pcvss, &pcvsr, stas);
    setisb(&rtcm.nav, popt->rectype[0], popt->rectype[1], stas, stas + 1);
    if (popt->tidecorr == 3 && fopt->blq[0] && !readotlgrid(fopt->blq, &rtcm.nav)) {
        fprintf(stderr, "blq grid file error: %s\n", fopt->blq);
        return 0;
    }
    if (!outhead((char *)outfile, NULL, 0, popt, sopt)) return 0;
    for (i = 0; i < MAXRCV; i++) rtcm.nav.stas[i] = stas[i];
    rtkinitppprtk(&rtk, popt);
    return 1;
}

static SOCKET connect_tcp(const char *addr, int port)
{
    struct sockaddr_in sa = {0};
    struct hostent *host;
    SOCKET sock;

    if (!(host = gethostbyname(addr))) return INVALID_SOCKET;
    sa.sin_family = AF_INET;
    sa.sin_port = htons((u_short)port);
    memcpy(&sa.sin_addr, host->h_addr, host->h_length);
    if ((sock = socket(AF_INET, SOCK_STREAM, 0)) == INVALID_SOCKET) return INVALID_SOCKET;
    if (connect(sock, (struct sockaddr *)&sa, sizeof(sa)) != 0) {
        closesocket(sock);
        return INVALID_SOCKET;
    }
    return sock;
}

static void print_stat(uint32_t start, long nbyte)
{
    char tstr[64];
    double pos[3] = {0};

    time2str(rtk.sol.time, tstr, 1);
    if (rtk.sol.stat != SOLQ_NONE) ecef2pos(rtk.sol.rr, pos);
    printf("STAT %s %d %d %.9f %.9f %.4f %.0f %ld %d\n", tstr, rtk.sol.stat, rtk.sol.ns,
           pos[0] * R2D, pos[1] * R2D, pos[2], (tickget() - start) / 1000.0, nbyte,
           l6q.svid ? l6q.svid - 180 + MINPRNQZS - 1 : 0);
    fflush(stdout);
}

int main(int argc, char **argv)
{
    prcopt_t prcopt = prcopt_default;
    solopt_t solopt = solopt_default;
    filopt_t filopt = {""};
    char *conf = NULL, *outfile = NULL, *addr = "127.0.0.1", buff[4096], tracefile[1100];
    char *infile = NULL, *dumpfile = NULL;
    double delay = DEFAULT_DELAY;
    int i, n, port = 28785, level = 0, connected = 0;
    long nbyte = 0;
    uint32_t start = tickget(), tstat = start, tconn = 0;
    SOCKET sock = INVALID_SOCKET;
    FILE *fp;
    WSADATA wsa;
    fd_set rs;
    struct timeval tv;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-k") && i + 1 < argc) conf = argv[++i];
        else if (!strcmp(argv[i], "-o") && i + 1 < argc) outfile = argv[++i];
        else if (!strcmp(argv[i], "-p") && i + 1 < argc) port = atoi(argv[++i]);
        else if (!strcmp(argv[i], "-a") && i + 1 < argc) addr = argv[++i];
        else if (!strcmp(argv[i], "-x") && i + 1 < argc) level = atoi(argv[++i]);
        else if (!strcmp(argv[i], "-f") && i + 1 < argc) infile = argv[++i];
        else if (!strcmp(argv[i], "-d") && i + 1 < argc) dumpfile = argv[++i];
        else if (!strcmp(argv[i], "-l") && i + 1 < argc) delay = atof(argv[++i]);
    }
    if (!conf || !outfile) {
        fprintf(stderr, "usage: clas_rt -k config -p port -o output.pos [-a addr] [-x trace] [-l delay]\n"
                        "       clas_rt -k config -f input.sbf -o output.pos [-d obsdump] [-l delay]\n");
        return 2;
    }
    prcopt.mode = PMODE_PPP_RTK;
    prcopt.navsys = SYS_GPS | SYS_QZS;
    prcopt.refpos = 1;
    prcopt.glomodear = 1;
    solopt.timef = 0;
    sprintf(solopt.prog, "clas_rt ver.%s", VER_RTKLIB);
    resetsysopts();
    if (!loadopts(conf, sysopts)) {
        fprintf(stderr, "config read error: %s\n", conf);
        return 2;
    }
    getsysopts(&prcopt, &solopt, &filopt);
    prcopt.clas_auth = 0; /* L6E(認証)は使わない */
    if (level > 0) {
        sprintf(tracefile, "%s.trace", outfile);
        traceopen(tracefile);
        tracelevel(level);
    }
    if (!setup(&prcopt, &solopt, &filopt, outfile)) return 1;
    if (!(fp = fopen(outfile, "ab"))) {
        fprintf(stderr, "output file open error: %s\n", outfile);
        return 1;
    }
    if (dumpfile && !(fpdump = fopen(dumpfile, "w"))) return 1;
    if (infile) { /* 記録済みの SBF をまとめて処理する(試験用) */
        FILE *fin = fopen(infile, "rb");
        if (!fin) {
            fprintf(stderr, "input file open error: %s\n", infile);
            return 1;
        }
        while ((n = (int)fread(buff, 1, sizeof(buff), fin)) > 0) {
            for (i = 0; i < n; i++) {
                if (input_sbf(&sbf, (uint8_t)buff[i]) == 1) procdelayed(fp, &prcopt, &solopt, delay, 0);
            }
        }
        fclose(fin);
        procdelayed(fp, &prcopt, &solopt, delay, 1);
        fclose(fp);
        if (fpdump) fclose(fpdump);
        traceclose();
        return 0;
    }
    WSAStartup(MAKEWORD(2, 0), &wsa);
    signal(SIGINT, onsignal);
    signal(SIGTERM, onsignal);
#ifdef SIGBREAK
    signal(SIGBREAK, onsignal);
#endif
    printf("START %s\n", conf);
    fflush(stdout);
    while (!stopflag) {
        if (sock == INVALID_SOCKET) {
            if (tconn == 0 || tickget() - tconn >= 2000) { /* 2 秒ごとに再接続 */
                tconn = tickget();
                sock = connect_tcp(addr, port);
                if (sock != INVALID_SOCKET && !connected) {
                    printf("CONNECTED %s:%d\n", addr, port);
                    fflush(stdout);
                    connected = 1;
                }
            }
            if (sock == INVALID_SOCKET) sleepms(100);
        }
        if (sock != INVALID_SOCKET) {
            FD_ZERO(&rs);
            FD_SET(sock, &rs);
            tv.tv_sec = 0;
            tv.tv_usec = 200000;
            if (select(0, &rs, NULL, NULL, &tv) > 0) {
                if ((n = recv(sock, buff, sizeof(buff), 0)) <= 0) {
                    closesocket(sock);
                    sock = INVALID_SOCKET;
                } else {
                    nbyte += n;
                    for (i = 0; i < n; i++) {
                        if (input_sbf(&sbf, (uint8_t)buff[i]) == 1) procdelayed(fp, &prcopt, &solopt, delay, 0);
                    }
                }
            }
        }
        if (tickget() - tstat >= 1000) {
            tstat = tickget();
            print_stat(start, nbyte);
        }
    }
    if (sock != INVALID_SOCKET) closesocket(sock);
    fclose(fp);
    rtkfreeppprtk(&rtk);
    traceclose();
    WSACleanup();
    printf("STOP\n");
    fflush(stdout);
    return 0;
}
