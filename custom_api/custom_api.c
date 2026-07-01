/*
 * custom_api.c — мінімальний REST HTTP-сервер для зміни /etc/waybeam.json
 *
 * Збірка (arm-linux-gnueabihf або на камері):
 *   gcc -O2 -o custom_api custom_api.c
 *
 * Використання:
 *   ./custom_api [port]          # default port 8765
 *
 * Ендпоінти:
 *   GET /set?size=1024x576&framing=off
 *   GET /set?size=1920x1080
 *   GET /set?framing=zoom-1.25x
 *   GET /set?mirror=true&flip=false
 *   GET /set?size=2560x1440&framing=zoom-2x&mirror=true&flip=true
 *   GET /get          -- повертає поточні значення з конфіга
 *   GET /health       -- перевірка що сервіс живий
 *
 * Відповідь завжди JSON: {"ok":true} або {"ok":false,"error":"..."}
 *
 * Дозволені значення framing:
 *   off, stab, stab-fill,
 *   zoom-1.25x, zoom-1.50x, zoom-1.75x, zoom-2x, zoom-3x, zoom-4x
 *
 * Формат size: ШxВ  (наприклад 1920x1080, 1280x720, 1024x576, auto)
 * Формат mirror/flip: true | false | 1 | 0
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <signal.h>
#include <fcntl.h>
#include <ctype.h>
#include <time.h>
#include <sys/time.h>
#include <stdint.h>
#include <pthread.h>

/* ─── Версія ──────────────────────────────────────────────────────── */
#define VERSION           "1.4wg"

/* ─── Налаштування ────────────────────────────────────────────────── */
#define DEFAULT_PORT      8765
#define CONFIG_PATH       "/etc/waybeam.json"
#define CONFIG_TMP        "/etc/waybeam.json.tmp"
#define RESTART_CMD       "/etc/init.d/S95waybeam restart"
#define MAX_REQUEST       4096
#define MAX_CONFIG        65536   /* максимальний розмір конфіга */
#define BACKLOG           8

/* ─── VPN Provisioning ─────────────────────────────────────────────── */
/* Можна перевизначити через Makefile: -DVPN_SERVER_IP='"x.x.x.x"'    */
#ifndef VPN_SERVER_IP
# define VPN_SERVER_IP   "176.108.4.228"
#endif
#ifndef VPN_SERVER_PORT
# define VPN_SERVER_PORT  49152
#endif
#ifndef VPN_API_TOKEN
# define VPN_API_TOKEN   "0284"
#endif
#define WG_CONF_PATH     "/etc/wireguard/wg0.conf"
#define WG_CONF_TMP      "/etc/wireguard/wg0.conf.tmp"
#define WG_IFACE         "wg0"
#define VPN_RESP_MAX     8192

/* ─── Телеметрія / MSP ────────────────────────────────────────────── */
#define TELEMETRY_PORT        9001
#define MSP2_GET_TEXT         0x3006  /* Betaflight: читає текстове поле */
#define MSP2TEXT_PILOT_NAME   1       /* тип поля: pilot name */

static uint8_t crc8_dvb_s2(uint8_t crc, uint8_t b) {
    crc ^= b;
    for (int i = 0; i < 8; i++)
        crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0xD5) : (uint8_t)(crc << 1);
    return crc;
}

/* ─── Глобальний стан: номер екіпажу (заповнюється з телеметрії) ─── */
static int             g_crew_id   = -1;
static pthread_mutex_t g_crew_lock = PTHREAD_MUTEX_INITIALIZER;

/* ─── Допустимі значення framing ─────────────────────────────────── */
static const char *VALID_FRAMING[] = {
    "off", "stab", "stab-fill",
    "zoom-1.25x", "zoom-1.50x", "zoom-1.75x",
    "zoom-2x", "zoom-3x", "zoom-4x",
    NULL
};

/* ─── Логування ───────────────────────────────────────────────────── */
#define LOG(fmt, ...) \
    do { \
        time_t _t = time(NULL); \
        struct tm *_tm = localtime(&_t); \
        char _ts[20]; \
        strftime(_ts, sizeof(_ts), "%H:%M:%S", _tm); \
        fprintf(stderr, "[%s] " fmt "\n", _ts, ##__VA_ARGS__); \
    } while(0)

/* ══════════════════════════════════════════════════════════════════
 * URL decode: %XX → char, + → space
 * ══════════════════════════════════════════════════════════════════ */
static void url_decode(const char *src, char *dst, size_t dst_sz)
{
    size_t i = 0;
    while (*src && i + 1 < dst_sz) {
        if (*src == '%' && isxdigit((unsigned char)src[1]) && isxdigit((unsigned char)src[2])) {
            char hex[3] = { src[1], src[2], 0 };
            dst[i++] = (char)strtol(hex, NULL, 16);
            src += 3;
        } else if (*src == '+') {
            dst[i++] = ' ';
            src++;
        } else {
            dst[i++] = *src++;
        }
    }
    dst[i] = '\0';
}

/* ══════════════════════════════════════════════════════════════════
 * Витягти значення параметра з query string
 * Повертає 1 якщо знайдено, 0 якщо ні
 * ══════════════════════════════════════════════════════════════════ */
static int query_get(const char *qs, const char *key, char *val, size_t val_sz)
{
    size_t klen = strlen(key);
    const char *p = qs;
    while (p && *p) {
        /* пропускаємо роздільник */
        while (*p == '&') p++;
        /* перевіряємо ключ */
        if (strncmp(p, key, klen) == 0 && p[klen] == '=') {
            const char *v = p + klen + 1;
            const char *end = strchr(v, '&');
            size_t vlen = end ? (size_t)(end - v) : strlen(v);
            if (vlen >= val_sz) vlen = val_sz - 1;
            char raw[256] = {0};
            if (vlen >= sizeof(raw)) vlen = sizeof(raw) - 1;
            memcpy(raw, v, vlen);
            raw[vlen] = '\0';
            url_decode(raw, val, val_sz);
            return 1;
        }
        /* наступний параметр */
        p = strchr(p, '&');
        if (p) p++;
    }
    return 0;
}

/* ══════════════════════════════════════════════════════════════════
 * Валідація framing
 * ══════════════════════════════════════════════════════════════════ */
static int is_valid_framing(const char *v)
{
    for (int i = 0; VALID_FRAMING[i]; i++)
        if (strcmp(v, VALID_FRAMING[i]) == 0) return 1;
    return 0;
}

/* ══════════════════════════════════════════════════════════════════
 * Валідація size: auto | ЧИСЛОxЧИСЛО | 720p | 1080p
 * ══════════════════════════════════════════════════════════════════ */
static int is_valid_size(const char *v)
{
    if (strcmp(v, "auto") == 0) return 1;
    if (strcmp(v, "720p")  == 0) return 1;
    if (strcmp(v, "1080p") == 0) return 1;
    /* ЧИСЛОxЧИСЛО */
    char *p;
    long w = strtol(v, &p, 10);
    if (p == v || (*p != 'x' && *p != 'X')) return 0;
    p++;
    char *q;
    long h = strtol(p, &q, 10);
    if (q == p || *q != '\0') return 0;
    if (w < 64 || w > 4096 || h < 64 || h > 4096) return 0;
    return 1;
}

/* ══════════════════════════════════════════════════════════════════
 * Читання файлу в malloc-буфер. Повертає NULL при помилці.
 * Розмір кладе у *out_len.
 * ══════════════════════════════════════════════════════════════════ */
static char *read_file(const char *path, size_t *out_len)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz <= 0 || sz > MAX_CONFIG) { fclose(f); return NULL; }
    char *buf = malloc((size_t)sz + 1);
    if (!buf) { fclose(f); return NULL; }
    if (fread(buf, 1, (size_t)sz, f) != (size_t)sz) {
        free(buf); fclose(f); return NULL;
    }
    buf[sz] = '\0';
    *out_len = (size_t)sz;
    fclose(f);
    return buf;
}

/* ══════════════════════════════════════════════════════════════════
 * Замінити значення JSON-рядкового поля в "відео0" блоці.
 *
 * Шукаємо патерн:  "KEY"   :   "OLDVAL"
 * і замінюємо на:  "KEY"   :   "NEWVAL"
 *
 * Проста рядкова заміна без повноцінного JSON-парсингу.
 * Достатньо для добре-форматованого waybeam.json.
 *
 * Повертає нову рядок (malloc) або NULL при помилці.
 * ══════════════════════════════════════════════════════════════════ */
static char *json_replace_string_field(const char *json, const char *key,
                                        const char *newval, size_t *out_len)
{
    /* Будуємо ключ пошуку: "key" */
    char search_key[64];
    snprintf(search_key, sizeof(search_key), "\"%s\"", key);

    const char *pos = json;
    const char *found = NULL;
    const char *val_start = NULL;
    const char *val_end   = NULL;

    /* Шукаємо всі входження ключа та беремо перше де є значення-рядок */
    while ((pos = strstr(pos, search_key)) != NULL) {
        const char *after_key = pos + strlen(search_key);
        /* пропускаємо пробіли та : */
        while (*after_key == ' ' || *after_key == '\t' || *after_key == '\r'
               || *after_key == '\n') after_key++;
        if (*after_key != ':') { pos++; continue; }
        after_key++;
        while (*after_key == ' ' || *after_key == '\t' || *after_key == '\r'
               || *after_key == '\n') after_key++;
        if (*after_key != '"') { pos++; continue; }
        /* знайшли відкриваючу лапку значення */
        val_start = after_key; /* вказує на " */
        const char *v = val_start + 1;
        /* шукаємо закриваючу лапку (без escaping для простоти) */
        while (*v && *v != '"') v++;
        if (!*v) { pos++; continue; }
        val_end = v + 1; /* після закриваючої " */
        found = pos;
        break;
    }

    if (!found || !val_start || !val_end) return NULL;

    /* Збираємо новий рядок */
    /* Фрагменти: [json..val_start] + "newval" + [val_end..end] */
    size_t prefix_len = (size_t)(val_start - json);
    size_t suffix_len = strlen(val_end);
    size_t new_val_quoted_len = 1 + strlen(newval) + 1; /* "newval" */
    size_t total = prefix_len + new_val_quoted_len + suffix_len + 1;

    char *result = malloc(total);
    if (!result) return NULL;

    char *p = result;
    memcpy(p, json, prefix_len);          p += prefix_len;
    *p++ = '"';
    size_t nvl = strlen(newval);
    memcpy(p, newval, nvl);               p += nvl;
    *p++ = '"';
    memcpy(p, val_end, suffix_len);       p += suffix_len;
    *p = '\0';

    *out_len = (size_t)(p - result);
    return result;
}

/* ══════════════════════════════════════════════════════════════════
 * Витягти поточне значення рядкового поля з JSON
 * ══════════════════════════════════════════════════════════════════ */
static int json_get_string_field(const char *json, const char *key,
                                  char *val, size_t val_sz)
{
    char search_key[64];
    snprintf(search_key, sizeof(search_key), "\"%s\"", key);

    const char *pos = json;
    while ((pos = strstr(pos, search_key)) != NULL) {
        const char *after = pos + strlen(search_key);
        while (*after == ' ' || *after == '\t') after++;
        if (*after != ':') { pos++; continue; }
        after++;
        while (*after == ' ' || *after == '\t') after++;
        if (*after != '"') { pos++; continue; }
        after++; /* пропускаємо відкриваючу лапку */
        const char *v = after;
        while (*v && *v != '"') v++;
        size_t len = (size_t)(v - after);
        if (len >= val_sz) len = val_sz - 1;
        memcpy(val, after, len);
        val[len] = '\0';
        return 1;
    }
    return 0;
}

/* ══════════════════════════════════════════════════════════════════
 * Замінити значення JSON-булевого поля (true/false, без лапок).
 * Повертає нову рядок (malloc) або NULL при помилці.
 * ══════════════════════════════════════════════════════════════════ */
static char *json_replace_bool_field(const char *json, const char *key,
                                      int newval, size_t *out_len)
{
    char search_key[64];
    snprintf(search_key, sizeof(search_key), "\"%s\"", key);

    const char *pos = json;
    const char *val_start = NULL;
    const char *val_end   = NULL;

    while ((pos = strstr(pos, search_key)) != NULL) {
        const char *after = pos + strlen(search_key);
        while (*after == ' ' || *after == '\t' || *after == '\r' || *after == '\n') after++;
        if (*after != ':') { pos++; continue; }
        after++;
        while (*after == ' ' || *after == '\t' || *after == '\r' || *after == '\n') after++;
        if (strncmp(after, "true", 4) == 0) {
            val_start = after; val_end = after + 4; break;
        }
        if (strncmp(after, "false", 5) == 0) {
            val_start = after; val_end = after + 5; break;
        }
        pos++;
    }
    if (!val_start) return NULL;

    const char *new_str  = newval ? "true" : "false";
    size_t      new_len2 = strlen(new_str);
    size_t prefix_len    = (size_t)(val_start - json);
    size_t suffix_len    = strlen(val_end);
    size_t total         = prefix_len + new_len2 + suffix_len + 1;

    char *result = malloc(total);
    if (!result) return NULL;
    char *p = result;
    memcpy(p, json, prefix_len);    p += prefix_len;
    memcpy(p, new_str, new_len2);   p += new_len2;
    memcpy(p, val_end, suffix_len); p += suffix_len;
    *p = '\0';
    *out_len = (size_t)(p - result);
    return result;
}

/* ══════════════════════════════════════════════════════════════════
 * Прочитати булеве поле з JSON. Повертає 1/0 або -1 якщо не знайдено.
 * ══════════════════════════════════════════════════════════════════ */
static int json_get_bool_field(const char *json, const char *key)
{
    char search_key[64];
    snprintf(search_key, sizeof(search_key), "\"%s\"", key);
    const char *pos = json;
    while ((pos = strstr(pos, search_key)) != NULL) {
        const char *after = pos + strlen(search_key);
        while (*after == ' ' || *after == '\t') after++;
        if (*after != ':') { pos++; continue; }
        after++;
        while (*after == ' ' || *after == '\t') after++;
        if (strncmp(after, "true",  4) == 0) return 1;
        if (strncmp(after, "false", 5) == 0) return 0;
        pos++;
    }
    return -1;
}

/* ══════════════════════════════════════════════════════════════════
 * Парсинг булевого URL-параметра: true/1 → 1, false/0 → 0, інше → -1
 * ══════════════════════════════════════════════════════════════════ */
static int parse_bool_param(const char *s)
{
    if (strcmp(s, "true")  == 0 || strcmp(s, "1") == 0) return 1;
    if (strcmp(s, "false") == 0 || strcmp(s, "0") == 0) return 0;
    return -1;
}

/* ══════════════════════════════════════════════════════════════════
 * Записати буфер у файл атомарно (через tmp + rename)
 * ══════════════════════════════════════════════════════════════════ */
static int write_file_atomic(const char *path, const char *tmp_path,
                              const char *data, size_t len)
{
    FILE *f = fopen(tmp_path, "wb");
    if (!f) return -1;
    if (fwrite(data, 1, len, f) != len) { fclose(f); unlink(tmp_path); return -1; }
    fflush(f);
    fsync(fileno(f));
    fclose(f);
    if (rename(tmp_path, path) != 0) { unlink(tmp_path); return -1; }
    return 0;
}

/* ══════════════════════════════════════════════════════════════════
 * Відправити HTTP-відповідь
 * ══════════════════════════════════════════════════════════════════ */
static void send_response(int fd, int status_code, const char *status_text,
                           const char *body)
{
    char header[256];
    int hlen = snprintf(header, sizeof(header),
        "HTTP/1.1 %d %s\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %zu\r\n"
        "Connection: close\r\n"
        "\r\n",
        status_code, status_text, strlen(body));
    send(fd, header, (size_t)hlen, 0);
    send(fd, body, strlen(body), 0);
}

/* ══════════════════════════════════════════════════════════════════
 * Обробка запиту GET /set
 * ══════════════════════════════════════════════════════════════════ */
static void handle_set(int fd, const char *qs)
{
    char size_val[64]    = {0};
    char framing_val[64] = {0};
    char mirror_str[16]  = {0};
    char flip_str[16]    = {0};

    int has_size    = query_get(qs, "size",    size_val,   sizeof(size_val));
    int has_framing = query_get(qs, "framing", framing_val,sizeof(framing_val));
    int has_mirror  = query_get(qs, "mirror",  mirror_str, sizeof(mirror_str));
    int has_flip    = query_get(qs, "flip",    flip_str,   sizeof(flip_str));

    if (!has_size && !has_framing && !has_mirror && !has_flip) {
        send_response(fd, 400, "Bad Request",
            "{\"ok\":false,\"error\":\"need at least one of: size, framing, mirror, flip\"}");
        return;
    }

    /* Валідація */
    if (has_size && !is_valid_size(size_val)) {
        char err[128];
        snprintf(err, sizeof(err),
            "{\"ok\":false,\"error\":\"invalid size: %s\"}", size_val);
        send_response(fd, 400, "Bad Request", err);
        return;
    }
    if (has_framing && !is_valid_framing(framing_val)) {
        char err[256];
        snprintf(err, sizeof(err),
            "{\"ok\":false,\"error\":\"invalid framing: %s. "
            "Allowed: off stab stab-fill zoom-1.25x zoom-1.50x "
            "zoom-1.75x zoom-2x zoom-3x zoom-4x\"}", framing_val);
        send_response(fd, 400, "Bad Request", err);
        return;
    }
    int mirror_bool = 0, flip_bool = 0;
    if (has_mirror) {
        mirror_bool = parse_bool_param(mirror_str);
        if (mirror_bool < 0) {
            char err[128];
            snprintf(err, sizeof(err),
                "{\"ok\":false,\"error\":\"invalid mirror: %s. Use true or false\"}", mirror_str);
            send_response(fd, 400, "Bad Request", err);
            return;
        }
    }
    if (has_flip) {
        flip_bool = parse_bool_param(flip_str);
        if (flip_bool < 0) {
            char err[128];
            snprintf(err, sizeof(err),
                "{\"ok\":false,\"error\":\"invalid flip: %s. Use true or false\"}", flip_str);
            send_response(fd, 400, "Bad Request", err);
            return;
        }
    }

    /* Читаємо конфіг */
    size_t cfg_len;
    char *cfg = read_file(CONFIG_PATH, &cfg_len);
    if (!cfg) {
        char err[128];
        snprintf(err, sizeof(err),
            "{\"ok\":false,\"error\":\"cannot read %s: %s\"}",
            CONFIG_PATH, strerror(errno));
        send_response(fd, 500, "Internal Server Error", err);
        return;
    }

    char old_size[64]    = "?";
    char old_framing[64] = "?";
    json_get_string_field(cfg, "size",    old_size,    sizeof(old_size));
    json_get_string_field(cfg, "framing", old_framing, sizeof(old_framing));
    int old_mirror = json_get_bool_field(cfg, "mirror");
    int old_flip   = json_get_bool_field(cfg, "flip");

    /* Патчимо size */
    if (has_size) {
        size_t new_len;
        char *patched = json_replace_string_field(cfg, "size", size_val, &new_len);
        if (!patched) {
            free(cfg);
            send_response(fd, 500, "Internal Server Error",
                "{\"ok\":false,\"error\":\"field \\\"size\\\" not found in config\"}");
            return;
        }
        free(cfg); cfg = patched; cfg_len = new_len;
    }

    /* Патчимо framing */
    if (has_framing) {
        size_t new_len;
        char *patched = json_replace_string_field(cfg, "framing", framing_val, &new_len);
        if (!patched) {
            free(cfg);
            send_response(fd, 500, "Internal Server Error",
                "{\"ok\":false,\"error\":\"field \\\"framing\\\" not found in config\"}");
            return;
        }
        free(cfg); cfg = patched; cfg_len = new_len;
    }

    /* Патчимо mirror */
    if (has_mirror) {
        size_t new_len;
        char *patched = json_replace_bool_field(cfg, "mirror", mirror_bool, &new_len);
        if (!patched) {
            free(cfg);
            send_response(fd, 500, "Internal Server Error",
                "{\"ok\":false,\"error\":\"field \\\"mirror\\\" not found in config\"}");
            return;
        }
        free(cfg); cfg = patched; cfg_len = new_len;
    }

    /* Патчимо flip */
    if (has_flip) {
        size_t new_len;
        char *patched = json_replace_bool_field(cfg, "flip", flip_bool, &new_len);
        if (!patched) {
            free(cfg);
            send_response(fd, 500, "Internal Server Error",
                "{\"ok\":false,\"error\":\"field \\\"flip\\\" not found in config\"}");
            return;
        }
        free(cfg); cfg = patched; cfg_len = new_len;
    }

    /* Записуємо конфіг */
    if (write_file_atomic(CONFIG_PATH, CONFIG_TMP, cfg, cfg_len) != 0) {
        free(cfg);
        char err[128];
        snprintf(err, sizeof(err),
            "{\"ok\":false,\"error\":\"cannot write config: %s\"}",
            strerror(errno));
        send_response(fd, 500, "Internal Server Error", err);
        return;
    }
    free(cfg);

    #define BOOL_STR(v) ((v) == 1 ? "true" : (v) == 0 ? "false" : "?")
    LOG("config updated: size %s->%s  framing %s->%s  mirror %s->%s  flip %s->%s",
        has_size    ? old_size              : "-", has_size    ? size_val              : "-",
        has_framing ? old_framing           : "-", has_framing ? framing_val           : "-",
        has_mirror  ? BOOL_STR(old_mirror)  : "-", has_mirror  ? BOOL_STR(mirror_bool) : "-",
        has_flip    ? BOOL_STR(old_flip)    : "-", has_flip    ? BOOL_STR(flip_bool)   : "-");
    #undef BOOL_STR

    /* Перезапускаємо сервіс */
    LOG("running: %s", RESTART_CMD);
    int rc = system(RESTART_CMD);
    if (rc != 0) {
        char err[128];
        snprintf(err, sizeof(err),
            "{\"ok\":false,\"error\":\"config saved but restart failed (rc=%d)\"}", rc);
        send_response(fd, 500, "Internal Server Error", err);
        return;
    }

    /* Формуємо відповідь */
    char resp[768];
    char size_part[128]    = "";
    char framing_part[128] = "";
    char mirror_part[128]  = "";
    char flip_part[128]    = "";
    int  n_changes = 0;
    #define SEP (n_changes++ ? "," : "")
    if (has_size)
        snprintf(size_part, sizeof(size_part),
            "%s\"size\":{\"old\":\"%s\",\"new\":\"%s\"}", SEP, old_size, size_val);
    if (has_framing)
        snprintf(framing_part, sizeof(framing_part),
            "%s\"framing\":{\"old\":\"%s\",\"new\":\"%s\"}", SEP, old_framing, framing_val);
    #define BOOL_JSON(v) ((v) == 1 ? "true" : (v) == 0 ? "false" : "null")
    if (has_mirror)
        snprintf(mirror_part, sizeof(mirror_part),
            "%s\"mirror\":{\"old\":%s,\"new\":%s}",
            SEP, BOOL_JSON(old_mirror), mirror_bool ? "true" : "false");
    if (has_flip)
        snprintf(flip_part, sizeof(flip_part),
            "%s\"flip\":{\"old\":%s,\"new\":%s}",
            SEP, BOOL_JSON(old_flip), flip_bool ? "true" : "false");
    #undef SEP
    #undef BOOL_JSON
    snprintf(resp, sizeof(resp),
        "{\"ok\":true,\"changes\":{%s%s%s%s}}", size_part, framing_part, mirror_part, flip_part);

    send_response(fd, 200, "OK", resp);
}

/* ══════════════════════════════════════════════════════════════════
 * Обробка запиту GET /get
 * ══════════════════════════════════════════════════════════════════ */
static void handle_get(int fd)
{
    size_t cfg_len;
    char *cfg = read_file(CONFIG_PATH, &cfg_len);
    if (!cfg) {
        char err[128];
        snprintf(err, sizeof(err),
            "{\"ok\":false,\"error\":\"cannot read %s: %s\"}",
            CONFIG_PATH, strerror(errno));
        send_response(fd, 500, "Internal Server Error", err);
        return;
    }
    char size_val[64]    = "unknown";
    char framing_val[64] = "unknown";
    json_get_string_field(cfg, "size",    size_val,    sizeof(size_val));
    json_get_string_field(cfg, "framing", framing_val, sizeof(framing_val));
    int mirror_val = json_get_bool_field(cfg, "mirror");
    int flip_val   = json_get_bool_field(cfg, "flip");
    free(cfg);

    #define BOOL_JSON(v) ((v) == 1 ? "true" : (v) == 0 ? "false" : "null")
    char resp[256];
    snprintf(resp, sizeof(resp),
        "{\"ok\":true,\"video0\":{\"size\":\"%s\",\"framing\":\"%s\"},"
        "\"image\":{\"mirror\":%s,\"flip\":%s}}",
        size_val, framing_val, BOOL_JSON(mirror_val), BOOL_JSON(flip_val));
    #undef BOOL_JSON
    send_response(fd, 200, "OK", resp);
}

/* ══════════════════════════════════════════════════════════════════
 * Обробка одного клієнтського з'єднання
 * ══════════════════════════════════════════════════════════════════ */
static void handle_client(int fd)
{
    char req[MAX_REQUEST];
    ssize_t n = recv(fd, req, sizeof(req) - 1, 0);
    if (n <= 0) return;
    req[n] = '\0';

    /* Парсимо першу рядок: METHOD PATH HTTP/x.x */
    char method[16], path[512];
    if (sscanf(req, "%15s %511s", method, path) != 2) {
        send_response(fd, 400, "Bad Request", "{\"ok\":false,\"error\":\"bad request line\"}");
        return;
    }

    /* Розбиваємо path на шлях та query string */
    char *qs = strchr(path, '?');
    if (qs) { *qs = '\0'; qs++; } else { qs = ""; }

    LOG("< %s %s%s%s", method, path, *qs ? "?" : "", qs);

    if (strcmp(method, "GET") != 0) {
        send_response(fd, 405, "Method Not Allowed",
            "{\"ok\":false,\"error\":\"only GET is supported\"}");
        return;
    }

    if (strcmp(path, "/set") == 0) {
        handle_set(fd, qs);
    } else if (strcmp(path, "/get") == 0) {
        handle_get(fd);
    } else if (strcmp(path, "/health") == 0) {
        send_response(fd, 200, "OK", "{\"ok\":true,\"service\":\"custom_api\"}");
    } else {
        send_response(fd, 404, "Not Found",
            "{\"ok\":false,\"error\":\"unknown endpoint. "
            "Use /set?size=...&framing=... or /get or /health\"}");
    }
}

/* ══════════════════════════════════════════════════════════════════
 * main
 * ══════════════════════════════════════════════════════════════════ */
/* ══════════════════════════════════════════════════════════════════
 * Парсинг номера пілота з імені типу "GS3"
 * Повертає номер (>=0) або -1 якщо не знайдено.
 * ══════════════════════════════════════════════════════════════════ */
static int parse_pilot_number(const char *name)
{
    const char *p = name;

    while (*p) {
        if ((p[0] == 'G' || p[0] == 'g') &&
            (p[1] == 'S' || p[1] == 's')) {

            const char *num_start = p + 2;

            if (isdigit((unsigned char)*num_start)) {
                char *endp;
                long n = strtol(num_start, &endp, 10);

                if (endp != num_start && n >= 0 && n <= 9999)
                    return (int)n;
            }
        }

        p++;
    }

    return -1;
}
/* Forward declaration (vpn функції визначені нижче) */
static int vpn_reconfigure_for_crew(int crew_id, char *out_msg, size_t out_sz);

/* ══════════════════════════════════════════════════════════════════
 * Фоновий потік: читає craft name через MSP_NAME з ser2net (UDP 9001)
 * і логує знайдений номер пілота.
 * ══════════════════════════════════════════════════════════════════ */
static void *pilot_name_reader_thread(void *arg)
{
    (void)arg;

    /* Чекаємо, поки ser2net підніметься */
    sleep(3);

    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        LOG("pilot: socket() failed: %s", strerror(errno));
        return NULL;
    }

    struct sockaddr_in local = {
        .sin_family      = AF_INET,
        .sin_port        = htons(0),
        .sin_addr.s_addr = INADDR_ANY,
    };
    bind(sock, (struct sockaddr *)&local, sizeof(local));

    struct sockaddr_in srv = {
        .sin_family      = AF_INET,
        .sin_port        = htons(TELEMETRY_PORT),
        .sin_addr.s_addr = inet_addr("127.0.0.1"),
    };

    /* MSP v2 запит: $X< | flag(0) | cmd_lo | cmd_hi | size_lo | size_hi | payload | CRC8 */
    uint8_t req[10];
    req[0] = '$'; req[1] = 'X'; req[2] = '<';
    req[3] = 0;                                          /* flag */
    req[4] = (uint8_t)(MSP2_GET_TEXT & 0xFF);
    req[5] = (uint8_t)(MSP2_GET_TEXT >> 8);
    req[6] = 1; req[7] = 0;                             /* payload size = 1 */
    req[8] = MSP2TEXT_PILOT_NAME;                        /* payload */
    uint8_t req_crc = 0;
    for (int i = 3; i <= 8; i++) req_crc = crc8_dvb_s2(req_crc, req[i]);
    req[9] = req_crc;

    uint8_t rx_buf[256];

    for (int attempt = 0; attempt < 10; attempt++) {
        if (sendto(sock, req, sizeof(req), 0,
                   (struct sockaddr *)&srv, sizeof(srv)) < 0) {
            LOG("pilot: sendto failed: %s", strerror(errno));
            sleep(1);
            continue;
        }

        struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

        /* Збираємо відповідь; ser2net може надсилати кількома датаграмами */
        size_t rx_len = 0;
        time_t deadline = time(NULL) + 2;

        while (time(NULL) < deadline && rx_len < sizeof(rx_buf) - 1) {
            ssize_t n = recv(sock, rx_buf + rx_len,
                             sizeof(rx_buf) - 1 - rx_len, 0);
            if (n <= 0) break;
            rx_len += (size_t)n;

            /* Шукаємо MSP v2 response frame: $X>
             * Формат: $X> | flag(1) | cmd(2 LE) | size(2 LE) | payload | CRC8 */
            for (size_t idx = 0; idx + 7 < rx_len; idx++) {
                if (rx_buf[idx]   != '$') continue;
                if (rx_buf[idx+1] != 'X') continue;
                if (rx_buf[idx+2] != '>') continue;

                uint16_t cmd  = rx_buf[idx+4] | ((uint16_t)rx_buf[idx+5] << 8);
                uint16_t plen = rx_buf[idx+6] | ((uint16_t)rx_buf[idx+7] << 8);
                if (cmd != MSP2_GET_TEXT) continue;
                if (idx + 8 + plen + 1 > rx_len) continue; /* неповний */

                /* CRC8 охоплює: flag + cmd(2) + size(2) + payload */
                uint8_t cksum = 0;
                for (size_t j = idx + 3; j < idx + 8 + plen; j++)
                    cksum = crc8_dvb_s2(cksum, rx_buf[j]);
                if (cksum != rx_buf[idx + 8 + plen]) continue;

                /* Payload: байт 0 = text type, байти 1..plen-1 = рядок */
                if (plen < 1) continue;
                char name[64] = {0};
                size_t copy = (size_t)(plen - 1) < sizeof(name) - 1 ? (size_t)(plen - 1) : sizeof(name) - 1;
                memcpy(name, rx_buf + idx + 9, copy);

                int num = parse_pilot_number(name);
                if (num >= 0) {
                    LOG("pilot: name=\"%s\"  station_number=%d", name, num);
                    pthread_mutex_lock(&g_crew_lock);
                    g_crew_id = num;
                    pthread_mutex_unlock(&g_crew_lock);

                    char vpn_result[512];
                    vpn_reconfigure_for_crew(num, vpn_result, sizeof(vpn_result));
                    LOG("vpn: %s", vpn_result);
                } else {
                    LOG("pilot: name=\"%s\"  (number not parsed)", name);
                }

                close(sock);
                return NULL;
            }
        }

        LOG("pilot: attempt %d — no response yet", attempt + 1);
        sleep(1);
    }

    LOG("pilot: failed to read pilot name after 10 attempts");
    close(sock);
    return NULL;
}

/* ══════════════════════════════════════════════════════════════════
 * VPN: розекранування JSON-рядка (\n → newline, \\ → \, \" → ")
 * ══════════════════════════════════════════════════════════════════ */
static void json_unescape(const char *src, char *dst, size_t dst_sz)
{
    size_t i = 0;
    while (*src && i + 1 < dst_sz) {
        if (*src == '\\' && src[1]) {
            src++;
            switch (*src) {
                case 'n':  dst[i++] = '\n'; break;
                case 'r':  dst[i++] = '\r'; break;
                case 't':  dst[i++] = '\t'; break;
                case '\\': dst[i++] = '\\'; break;
                case '"':  dst[i++] = '"';  break;
                default:   dst[i++] = *src; break;
            }
            src++;
        } else {
            dst[i++] = *src++;
        }
    }
    dst[i] = '\0';
}

/* ══════════════════════════════════════════════════════════════════
 * VPN: відфільтрувати рядки Address та MTU з WireGuard-конфігу.
 * wg setconf не розуміє ці поля — вони потрібні тільки для wg-quick.
 * ══════════════════════════════════════════════════════════════════ */
static void wg_filter_config(const char *src, char *dst, size_t dst_sz)
{
    size_t out = 0;
    const char *line = src;

    while (*line && out < dst_sz - 1) {
        const char *eol = strchr(line, '\n');
        size_t line_len = eol ? (size_t)(eol - line) : strlen(line);

        int skip = (strncmp(line, "Address", 7) == 0 ||
                    strncmp(line, "MTU",     3) == 0);

        if (!skip && out + line_len + 1 < dst_sz) {
            memcpy(dst + out, line, line_len);
            out += line_len;
            dst[out++] = '\n';
        }

        if (!eol) break;
        line = eol + 1;
    }
    dst[out] = '\0';
}

/* ══════════════════════════════════════════════════════════════════
 * VPN: HTTP POST /provision → заповнює resp_buf, повертає довжину
 * або -1 при помилці з'єднання.
 * Використовує HTTP/1.0 з Connection: close — без chunked encoding.
 * ══════════════════════════════════════════════════════════════════ */
static int http_post_provision(int crew_id, char *resp_buf, size_t resp_sz)
{
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
        LOG("vpn: socket() failed: %s", strerror(errno));
        return -1;
    }

    struct timeval tv = { .tv_sec = 10, .tv_usec = 0 };
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    struct sockaddr_in srv;
    memset(&srv, 0, sizeof(srv));
    srv.sin_family = AF_INET;
    srv.sin_port   = htons(VPN_SERVER_PORT);
    if (inet_pton(AF_INET, VPN_SERVER_IP, &srv.sin_addr) != 1) {
        LOG("vpn: invalid server IP: %s", VPN_SERVER_IP);
        close(sock);
        return -1;
    }

    if (connect(sock, (struct sockaddr *)&srv, sizeof(srv)) != 0) {
        LOG("vpn: connect %s:%d failed: %s",
            VPN_SERVER_IP, VPN_SERVER_PORT, strerror(errno));
        close(sock);
        return -1;
    }

    char body[128];
    int blen = snprintf(body, sizeof(body),
        "{\"crew_id\":%d,\"role\":\"camera\"}", crew_id);

    char req[512];
    int rlen = snprintf(req, sizeof(req),
        "POST /provision HTTP/1.0\r\n"
        "Host: %s:%d\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %d\r\n"
        "X-API-Token: %s\r\n"
        "Connection: close\r\n"
        "\r\n"
        "%s",
        VPN_SERVER_IP, VPN_SERVER_PORT, blen, VPN_API_TOKEN, body);

    if (send(sock, req, (size_t)rlen, 0) < rlen) {
        LOG("vpn: send() failed: %s", strerror(errno));
        close(sock);
        return -1;
    }

    size_t total = 0;
    for (;;) {
        ssize_t n = recv(sock, resp_buf + total, resp_sz - 1 - total, 0);
        if (n <= 0) break;
        total += (size_t)n;
        if (total >= resp_sz - 1) break;
    }
    resp_buf[total] = '\0';
    close(sock);
    return (int)total;
}

/* ══════════════════════════════════════════════════════════════════
 * VPN: виконати shell-команду з логуванням
 * ══════════════════════════════════════════════════════════════════ */
static int vpn_run_cmd(const char *cmd)
{
    LOG("vpn: run: %s", cmd);
    int rc = system(cmd);
    if (rc != 0)
        LOG("vpn: cmd rc=%d: %s", rc, cmd);
    return rc;
}

/* ══════════════════════════════════════════════════════════════════
 * VPN: основна функція налаштування тунелю для конкретного crew_id.
 * out_msg отримує JSON-результат.
 * Повертає 0 при успіху, -1 при помилці.
 * ══════════════════════════════════════════════════════════════════ */
static int vpn_reconfigure_for_crew(int crew_id, char *out_msg, size_t out_sz)
{
    LOG("vpn: provisioning for crew_id=%d via %s:%d",
        crew_id, VPN_SERVER_IP, VPN_SERVER_PORT);

    /* ── 1. HTTP POST /provision ─────────────────────────────── */
    char *http_buf = malloc(VPN_RESP_MAX);
    if (!http_buf) {
        snprintf(out_msg, out_sz, "{\"ok\":false,\"error\":\"out of memory\"}");
        return -1;
    }

    int http_len = http_post_provision(crew_id, http_buf, VPN_RESP_MAX);
    if (http_len <= 0) {
        free(http_buf);
        snprintf(out_msg, out_sz,
            "{\"ok\":false,\"error\":\"cannot connect to provision server %s:%d\"}",
            VPN_SERVER_IP, VPN_SERVER_PORT);
        return -1;
    }

    /* ── 2. Знайти JSON-тіло після HTTP-заголовків ───────────── */
    const char *json_body = strstr(http_buf, "\r\n\r\n");
    if (!json_body) {
        free(http_buf);
        snprintf(out_msg, out_sz,
            "{\"ok\":false,\"error\":\"malformed HTTP response from provision server\"}");
        return -1;
    }
    json_body += 4;

    /* ── 3. Перевірити ok: true ──────────────────────────────── */
    if (json_get_bool_field(json_body, "ok") != 1) {
        char srv_err[256] = "server returned ok:false";
        json_get_string_field(json_body, "error", srv_err, sizeof(srv_err));
        free(http_buf);
        snprintf(out_msg, out_sz,
            "{\"ok\":false,\"error\":\"provision server: %s\"}", srv_err);
        return -1;
    }

    /* ── 4. Витягти поля відповіді ───────────────────────────── */
    char camera_ip[64]    = {0};
    char ground_ip[64]    = {0};
    char network[64]      = {0};
    char config_raw[4096] = {0};

    if (!json_get_string_field(json_body, "camera_ip", camera_ip, sizeof(camera_ip))) {
        free(http_buf);
        snprintf(out_msg, out_sz,
            "{\"ok\":false,\"error\":\"no camera_ip in provision response\"}");
        return -1;
    }
    if (!json_get_string_field(json_body, "ground_ip", ground_ip, sizeof(ground_ip))) {
        free(http_buf);
        snprintf(out_msg, out_sz,
            "{\"ok\":false,\"error\":\"no ground_ip in provision response\"}");
        return -1;
    }
    if (!json_get_string_field(json_body, "config", config_raw, sizeof(config_raw))) {
        free(http_buf);
        snprintf(out_msg, out_sz,
            "{\"ok\":false,\"error\":\"no config in provision response\"}");
        return -1;
    }
    json_get_string_field(json_body, "network", network, sizeof(network));
    free(http_buf);

    /* ── 5. Витягти довжину префікса з network (наприклад /24) ─ */
    char prefix[8] = "24";
    char *slash = strchr(network, '/');
    if (slash && isdigit((unsigned char)slash[1]))
        snprintf(prefix, sizeof(prefix), "%s", slash + 1);

    /* ── 6. Базова валідація щоб уникнути command injection ──── */
    for (const char *p = camera_ip; *p; p++) {
        if (!isdigit((unsigned char)*p) && *p != '.') {
            snprintf(out_msg, out_sz,
                "{\"ok\":false,\"error\":\"invalid camera_ip format: %s\"}", camera_ip);
            return -1;
        }
    }
    for (const char *p = ground_ip; *p; p++) {
        if (!isdigit((unsigned char)*p) && *p != '.') {
            snprintf(out_msg, out_sz,
                "{\"ok\":false,\"error\":\"invalid ground_ip format: %s\"}", ground_ip);
            return -1;
        }
    }
    for (const char *p = prefix; *p; p++) {
        if (!isdigit((unsigned char)*p)) {
            snprintf(out_msg, out_sz,
                "{\"ok\":false,\"error\":\"invalid network prefix: %s\"}", prefix);
            return -1;
        }
    }

    /* ── 7. Розекранувати JSON-рядок → реальні символи ──────── */
    char config_unesc[4096] = {0};
    json_unescape(config_raw, config_unesc, sizeof(config_unesc));

    /* ── 8. Відфільтрувати Address та MTU (wg setconf їх не знає) */
    char wg_conf[4096] = {0};
    wg_filter_config(config_unesc, wg_conf, sizeof(wg_conf));

    /* ── 9. Записати конфіг ──────────────────────────────────── */
    vpn_run_cmd("mkdir -p /etc/wireguard");
    if (write_file_atomic(WG_CONF_PATH, WG_CONF_TMP,
                          wg_conf, strlen(wg_conf)) != 0) {
        snprintf(out_msg, out_sz,
            "{\"ok\":false,\"error\":\"cannot write %s: %s\"}",
            WG_CONF_PATH, strerror(errno));
        return -1;
    }
    LOG("vpn: wrote %s", WG_CONF_PATH);

    /* ── 10. Зупинити старий wg0 (ігнорувати помилки — може не існувати) */
    vpn_run_cmd("ip link set " WG_IFACE " down 2>/dev/null || true");
    vpn_run_cmd("ip link del "  WG_IFACE " 2>/dev/null || true");

    /* ── 11. Створити новий інтерфейс ───────────────────────── */
    if (vpn_run_cmd("ip link add dev " WG_IFACE " type wireguard") != 0) {
        snprintf(out_msg, out_sz,
            "{\"ok\":false,\"error\":\"ip link add dev " WG_IFACE " type wireguard failed\"}");
        return -1;
    }
    if (vpn_run_cmd("wg setconf " WG_IFACE " " WG_CONF_PATH) != 0) {
        vpn_run_cmd("ip link del " WG_IFACE " 2>/dev/null || true");
        snprintf(out_msg, out_sz,
            "{\"ok\":false,\"error\":\"wg setconf " WG_IFACE " failed\"}");
        return -1;
    }

    /* ── 12. Призначити IP-адресу ────────────────────────────── */
    char ip_cmd[128];
    snprintf(ip_cmd, sizeof(ip_cmd),
        "ip address add %s/%s dev " WG_IFACE, camera_ip, prefix);
    if (vpn_run_cmd(ip_cmd) != 0) {
        vpn_run_cmd("ip link del " WG_IFACE " 2>/dev/null || true");
        snprintf(out_msg, out_sz,
            "{\"ok\":false,\"error\":\"ip address add %s/%s dev " WG_IFACE " failed\"}",
            camera_ip, prefix);
        return -1;
    }

    /* ── 13. Підняти інтерфейс ───────────────────────────────── */
    if (vpn_run_cmd("ip link set up dev " WG_IFACE) != 0) {
        snprintf(out_msg, out_sz,
            "{\"ok\":false,\"error\":\"ip link set up dev " WG_IFACE " failed\"}");
        return -1;
    }

    /* ── 14. Лог стану ───────────────────────────────────────── */
    vpn_run_cmd("wg show " WG_IFACE);
    vpn_run_cmd("ip addr show " WG_IFACE);

    /* ── 15. Оновити outgoing.server у waybeam ───────────────── */
    char waybeam_cmd[256];
    snprintf(waybeam_cmd, sizeof(waybeam_cmd),
        "wget -q -O /dev/null "
        "'http://127.0.0.1:4380/api/v1/set?outgoing.server=udp://%s:5600'",
        ground_ip);
    vpn_run_cmd(waybeam_cmd);

    LOG("vpn: crew_id=%d camera=%s ground=%s — " WG_IFACE " is up",
        crew_id, camera_ip, ground_ip);

    snprintf(out_msg, out_sz,
        "{\"ok\":true,\"crew_id\":%d,\"camera_ip\":\"%s\","
        "\"ground_ip\":\"%s\",\"iface\":\"" WG_IFACE "\"}",
        crew_id, camera_ip, ground_ip);
    return 0;
}

/* --- Дефолтні значення при старті --- */
#define DEFAULT_SIZE    "1024x576"
#define DEFAULT_FRAMING "off"

/*
 * reset_to_defaults() — скидає size та framing у конфізі до дефолтів.
 * Викликається один раз при старті демона.
 * waybeam НЕ перезапускається тут — він сам читає конфіг при власному старті.
 */
static void reset_to_defaults(void)
{
    size_t cfg_len;
    char *cfg = read_file(CONFIG_PATH, &cfg_len);
    if (!cfg) {
        LOG("reset: cannot read %s: %s", CONFIG_PATH, strerror(errno));
        return;
    }

    char cur_size[64]    = "?";
    char cur_framing[64] = "?";
    json_get_string_field(cfg, "size",    cur_size,    sizeof(cur_size));
    json_get_string_field(cfg, "framing", cur_framing, sizeof(cur_framing));

    size_t new_len;
    char *tmp = json_replace_string_field(cfg, "size", DEFAULT_SIZE, &new_len);
    if (!tmp) { LOG("reset: field \"size\" not found"); free(cfg); return; }
    free(cfg); cfg = tmp; cfg_len = new_len;

    tmp = json_replace_string_field(cfg, "framing", DEFAULT_FRAMING, &new_len);
    if (!tmp) { LOG("reset: field \"framing\" not found"); free(cfg); return; }
    free(cfg); cfg = tmp; cfg_len = new_len;

    if (write_file_atomic(CONFIG_PATH, CONFIG_TMP, cfg, cfg_len) != 0)
        LOG("reset: cannot write config: %s", strerror(errno));
    else
        LOG("reset: size %s->%s  framing %s->%s",
            cur_size, DEFAULT_SIZE, cur_framing, DEFAULT_FRAMING);

    free(cfg);
}

int main(int argc, char *argv[])
{
    int port = DEFAULT_PORT;
    if (argc >= 2) {
        port = atoi(argv[1]);
        if (port <= 0 || port > 65535) {
            fprintf(stderr, "Invalid port: %s\n", argv[1]);
            return 1;
        }
    }

    /* Скидаємо конфіг до дефолтів при кожному старті */
    reset_to_defaults();

    /* Читаємо ім'я пілота з телеметрії у фоні */
    {
        pthread_t pt;
        if (pthread_create(&pt, NULL, pilot_name_reader_thread, NULL) == 0)
            pthread_detach(pt);
        else
            LOG("pilot: pthread_create failed");
    }

    /* Ігноруємо SIGPIPE (клієнт закрив з'єднання) */
    signal(SIGPIPE, SIG_IGN);

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) { perror("socket"); return 1; }

    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr = {
        .sin_family      = AF_INET,
        .sin_port        = htons((uint16_t)port),
        .sin_addr.s_addr = INADDR_ANY,
    };
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind"); close(srv); return 1;
    }
    if (listen(srv, BACKLOG) < 0) {
        perror("listen"); close(srv); return 1;
    }

    LOG("custom_api v" VERSION " listening on port %d", port);
    LOG("config: %s", CONFIG_PATH);
    LOG("restart: %s", RESTART_CMD);

    for (;;) {
        struct sockaddr_in cli_addr;
        socklen_t cli_len = sizeof(cli_addr);
        int cli = accept(srv, (struct sockaddr *)&cli_addr, &cli_len);
        if (cli < 0) {
            if (errno == EINTR) continue;
            perror("accept");
            continue;
        }
        /* Таймаут читання 5 секунд */
        struct timeval tv = { .tv_sec = 5, .tv_usec = 0 };
        setsockopt(cli, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

        handle_client(cli);
        close(cli);
    }

    close(srv);
    return 0;
}