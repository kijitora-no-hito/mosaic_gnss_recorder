/* malib_rt : MALIB の rtksvr を GUI なしで動かすリアルタイム測位エンジン(Windows 用)
 *
 * 使い方: malib_rt 設定ファイル
 * 設定は rtkrcv 形式。入出力は設定の inpstr / outstr に従う。
 * 1 秒ごとに状態を標準出力へ 1 行出す。Ctrl+C / Ctrl+Break で止める。
 * 状態行: STAT 日付 時刻(GPST) Q 衛星数 緯度[deg] 経度[deg] 楕円体高[m] 経過秒 入力バイト数
 * 解が無いときは Q=0 で緯度経度高さは 0。
 *
 * rtkrcv(MALIB app/consapp/rtkrcv)の起動処理を元に、端末処理を除いて作った。
 * MALIB と同じ BSD 2 条項ライセンス(LICENSE.txt)に従う。
 */
#include <signal.h>
#include "rtklib.h"

#define MAXSTR 1024

static rtksvr_t *svr;
static stream_t moni;
static volatile int stopflag = 0;

static int strtype[MAXSTRRTK] = {STR_NONE};
static char strpath[MAXSTRRTK][MAXSTR] = {""};
static int strfmt[NSTRIN + NSTROUT] = {STRFMT_SEPT, STRFMT_RTCM3, STRFMT_SP3, STRFMT_SP3,
                                       SOLF_LLH, SOLF_NMEA};
static int strssr[NSTRIN] = {STRSSR_OFF, STRSSR_OFF, STRSSR_OFF, STRSSR_OFF};
static int svrcycle = 10, timeout = 10000, reconnect = 10000, nmeacycle = 5000;
static int buffsize = 32768, navmsgsel = 0, nmeareq = 0, fswapmargin = 30;
static double nmeapos[3] = {0}, maxdrtr = 0.0;
static char proxyaddr[256] = "";
static prcopt_t prcopt;
static solopt_t solopt[NSTROUT] = {{0}};
static filopt_t filopt = {""};

#define ISTOPT "0:off,1:serial,2:file,3:tcpsvr,4:tcpcli,6:ntripcli,7:ftp,8:http,10:udpsvr,11:udpcli"
#define OSTOPT "0:off,1:serial,2:file,3:tcpsvr,4:tcpcli,5:ntripsvr,9:ntripcas,10:udpsvr,11:udpcli"
#define FMTOPT "0:rtcm2,1:rtcm3,2:oem4,3:oem3,4:ubx,5:ss2,6:hemis,7:skytraq,8:javad,9:nvs,10:binex,11:rt17,12:sbf,14:sp3,15:l6,20:stat"
#define SOLOPT "0:llh,1:xyz,2:enu,3:nmea,4:stat"
#define SSROPT "-1:off,0:main,1:sub"

static opt_t rcvopts[] = {
    {"inpstr1-type",   3, (void *)&strtype[0], ISTOPT}, {"inpstr2-type", 3, (void *)&strtype[1], ISTOPT},
    {"inpstr3-type",   3, (void *)&strtype[2], ISTOPT}, {"inpstr4-type", 3, (void *)&strtype[3], ISTOPT},
    {"inpstr1-path",   2, (void *)strpath[0], ""},      {"inpstr2-path", 2, (void *)strpath[1], ""},
    {"inpstr3-path",   2, (void *)strpath[2], ""},      {"inpstr4-path", 2, (void *)strpath[3], ""},
    {"inpstr1-format", 3, (void *)&strfmt[0], FMTOPT},  {"inpstr2-format", 3, (void *)&strfmt[1], FMTOPT},
    {"inpstr3-format", 3, (void *)&strfmt[2], FMTOPT},  {"inpstr4-format", 3, (void *)&strfmt[3], FMTOPT},
    {"inpstr1-ssr",    3, (void *)&strssr[0], SSROPT},  {"inpstr2-ssr", 3, (void *)&strssr[1], SSROPT},
    {"inpstr3-ssr",    3, (void *)&strssr[2], SSROPT},  {"inpstr4-ssr", 3, (void *)&strssr[3], SSROPT},
    {"outstr1-type",   3, (void *)&strtype[NSTRIN], OSTOPT},
    {"outstr2-type",   3, (void *)&strtype[NSTRIN + 1], OSTOPT},
    {"outstr1-path",   2, (void *)strpath[NSTRIN], ""},
    {"outstr2-path",   2, (void *)strpath[NSTRIN + 1], ""},
    {"outstr1-format", 3, (void *)&strfmt[NSTRIN], SOLOPT},
    {"outstr2-format", 3, (void *)&strfmt[NSTRIN + 1], SOLOPT},
    {"misc-svrcycle",  0, (void *)&svrcycle, "ms"},     {"misc-timeout", 0, (void *)&timeout, "ms"},
    {"misc-reconnect", 0, (void *)&reconnect, "ms"},    {"misc-buffsize", 0, (void *)&buffsize, "bytes"},
    {"", 0, NULL, ""}
};

static void onsignal(int sig)
{
    stopflag = 1;
    signal(sig, onsignal);
}

/* 衛星・受信機アンテナの PCV を読む(rtkrcv と同じ) */
static void readant(void)
{
    const pcv_t pcv0 = {0};
    pcvs_t pcvr = {0}, pcvs = {0};
    pcv_t *pcv;
    gtime_t time = timeget();
    int i;

    prcopt.pcvr[0] = prcopt.pcvr[1] = pcv0;
    if (*filopt.rcvantp && readpcv(filopt.rcvantp, &pcvr)) {
        for (i = 0; i < 2; i++) {
            if (!*prcopt.anttype[i]) continue;
            if ((pcv = searchpcv(0, prcopt.anttype[i], time, &pcvr))) copypcv(pcv, &prcopt.pcvr[i]);
            else fprintf(stderr, "no antenna %s\n", prcopt.anttype[i]);
        }
    }
    if (*filopt.satantp && readpcv(filopt.satantp, &pcvs)) {
        for (i = 0; i < MAXSAT; i++) {
            if ((pcv = searchpcv(i + 1, "", time, &pcvs))) copypcv(pcv, &svr->nav.pcvs[i]);
        }
    } else {
        fprintf(stderr, "satellite antenna file open error: %s\n", filopt.satantp);
    }
    freepcv(&pcvr);
    freepcv(&pcvs);
}

int main(int argc, char **argv)
{
    char *paths[MAXSTRRTK], *cmds[NSTRIN] = {NULL}, *cmds_periodic[NSTRIN] = {NULL};
    char *ropts[NSTRIN] = {"", "", "", ""}, errmsg[2048] = "", tstr[64];
    int i, stropt[8] = {0};
    double npos[3] = {0}, pos[3];
    gtime_t rst = {0};
    uint32_t start = tickget();

    if (argc < 2) {
        fprintf(stderr, "usage: malib_rt config\n");
        return 2;
    }
    svr = (rtksvr_t *)calloc(1, sizeof(rtksvr_t));
    if (!svr) {
        fprintf(stderr, "memory error\n");
        return 1;
    }
    rtksvrinit(svr);
    strinit(&moni);
    resetsysopts();
    if (!loadopts(argv[1], rcvopts) || !loadopts(argv[1], sysopts)) {
        fprintf(stderr, "config read error: %s\n", argv[1]);
        return 2;
    }
    getsysopts(&prcopt, solopt, &filopt);
    for (i = 0; i < MAXSTRRTK; i++) paths[i] = strpath[i];
    readant();
    stropt[0] = timeout; stropt[1] = reconnect; stropt[2] = 1000;
    stropt[3] = buffsize; stropt[4] = fswapmargin;
    strsetopt(stropt);
    strsetproxy(proxyaddr);
    solopt[0].posf = strfmt[NSTRIN];
    solopt[1].posf = strfmt[NSTRIN + 1];
    if (!rtksvrstart(svr, svrcycle, buffsize, strtype, paths, strfmt, strssr, navmsgsel,
                     cmds, cmds_periodic, ropts, nmeacycle, nmeareq, npos, maxdrtr, &prcopt,
                     solopt, &moni, rst, errmsg, &filopt)) {
        fprintf(stderr, "rtk server start error (%s)\n", errmsg);
        return 1;
    }
    signal(SIGINT, onsignal);
    signal(SIGTERM, onsignal);
#ifdef SIGBREAK
    signal(SIGBREAK, onsignal);
#endif
    printf("START %s\n", argv[1]);
    fflush(stdout);
    while (!stopflag) {
        sleepms(1000);
        rtksvrlock(svr);
        time2str(svr->rtk.sol.time, tstr, 1);
        pos[0] = pos[1] = pos[2] = 0.0;
        if (svr->rtk.sol.stat != SOLQ_NONE) ecef2pos(svr->rtk.sol.rr, pos);
        printf("STAT %s %d %d %.9f %.9f %.4f %.0f %d\n", tstr, svr->rtk.sol.stat,
               svr->rtk.sol.ns, pos[0] * R2D, pos[1] * R2D, pos[2],
               (tickget() - start) / 1000.0, svr->nb[0]);
        rtksvrunlock(svr);
        fflush(stdout);
    }
    rtksvrstop(svr, cmds);
    printf("STOP\n");
    fflush(stdout);
    return 0;
}
