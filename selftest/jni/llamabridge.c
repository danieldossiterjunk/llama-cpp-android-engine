/*
 * llamabridge.c - minimal JNI bridge between the engine self-test Activity and
 * the prebuilt llama.cpp v1.0.0 libraries (libllama.so, libggml.so,
 * libggml-base.so, libggml-cpu.so).
 *
 * Purpose: validate the native engine on the phone (load, report CPU features
 * and page size, load a GGUF model, greedy-generate a few tokens, cancel,
 * release, repeat). It is NOT a chat application.
 *
 * Written in C on purpose: a C bridge does not make APK Builder add
 * libc++_shared.so, so the engine's own statically linked, hidden C++ runtime
 * is the only C++ runtime in the app.
 *
 * Compiled by APK Builder 004 (Clang 21, C17, -O2 -fPIC -fstack-protector-strong
 * -Wall, NDK API 26) and linked against the declared prebuilt libraries. The
 * headers in this folder MUST come from the same llama.cpp commit as the
 * libraries (v1.0.0, b1b770d90d94cad9e9ea5b993bdbc383a522fb28).
 *
 * JNI class: org.llamaengine.selftest.LlamaEngine (all methods static).
 */

#include <jni.h>
#include <android/log.h>

#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "llama.h"
#include "ggml.h"
#include "ggml-backend.h"
#include "ggml-cpu.h"

#define BRIDGE_TAG "llamabridge"
#define BRIDGE_VERSION "llamabridge 1.0 (engine self-test)"

struct engine {
    struct llama_model         *model;
    struct llama_context       *ctx;
    struct llama_sampler       *smpl;
    const struct llama_vocab   *vocab;
    atomic_int                  cancel;
    int32_t                     n_threads;
    uint32_t                    n_ctx;
};

static atomic_int g_backend_ready = 0;
static atomic_int g_live_handles = 0;

/* last bridge error and last llama.cpp/ggml error-level log line, shown by lastError() */
static pthread_mutex_t g_err_lock = PTHREAD_MUTEX_INITIALIZER;
static char g_last_error[512] = "no error";
static char g_last_engine_log[1024] = "";

/* ------------------------------------------------------------------ text buffer */

struct textbuf {
    char   *data;
    size_t  len;
    size_t  cap;
    bool    oom;
};

static void tb_init(struct textbuf *tb) {
    tb->cap = 4096;
    tb->len = 0;
    tb->oom = false;
    tb->data = (char *) malloc(tb->cap);
    if (tb->data == NULL) {
        tb->cap = 0;
        tb->oom = true;
    } else {
        tb->data[0] = '\0';
    }
}

static void tb_append(struct textbuf *tb, const char *s, size_t n) {
    if (tb->oom) {
        return;
    }
    if (tb->len + n + 1 > tb->cap) {
        size_t ncap = tb->cap ? tb->cap : 4096;
        while (tb->len + n + 1 > ncap) {
            ncap *= 2;
        }
        char *nd = (char *) realloc(tb->data, ncap);
        if (nd == NULL) {
            tb->oom = true;
            return;
        }
        tb->data = nd;
        tb->cap = ncap;
    }
    memcpy(tb->data + tb->len, s, n);
    tb->len += n;
    tb->data[tb->len] = '\0';
}

static void tb_printf(struct textbuf *tb, const char *fmt, ...) __attribute__((format(printf, 2, 3)));

static void tb_printf(struct textbuf *tb, const char *fmt, ...) {
    char tmp[1024];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof tmp, fmt, ap);
    va_end(ap);
    if (n < 0) {
        return;
    }
    if ((size_t) n >= sizeof tmp) {
        n = (int) sizeof tmp - 1;
    }
    tb_append(tb, tmp, (size_t) n);
}

static void tb_free(struct textbuf *tb) {
    free(tb->data);
    tb->data = NULL;
    tb->len = tb->cap = 0;
}

/* Return the buffer as a Java byte[] (UTF-8; the Java side decodes it, so a
 * token that ends in the middle of a UTF-8 sequence cannot break JNI). */
static jbyteArray tb_to_bytes(JNIEnv *env, struct textbuf *tb) {
    if (tb->oom || tb->data == NULL) {
        static const char msg[] = "llamabridge: out of memory while building the report";
        jbyteArray arr = (*env)->NewByteArray(env, (jsize) (sizeof msg - 1));
        if (arr != NULL) {
            (*env)->SetByteArrayRegion(env, arr, 0, (jsize) (sizeof msg - 1), (const jbyte *) msg);
        }
        return arr;
    }
    jbyteArray arr = (*env)->NewByteArray(env, (jsize) tb->len);
    if (arr != NULL && tb->len > 0) {
        (*env)->SetByteArrayRegion(env, arr, 0, (jsize) tb->len, (const jbyte *) tb->data);
    }
    return arr;
}

/* ------------------------------------------------------------------ logging */

static void set_error(const char *msg) {
    pthread_mutex_lock(&g_err_lock);
    snprintf(g_last_error, sizeof g_last_error, "%s", msg);
    pthread_mutex_unlock(&g_err_lock);
    __android_log_write(ANDROID_LOG_ERROR, BRIDGE_TAG, msg);
}

static void clear_error(void) {
    pthread_mutex_lock(&g_err_lock);
    snprintf(g_last_error, sizeof g_last_error, "no error");
    g_last_engine_log[0] = '\0';
    pthread_mutex_unlock(&g_err_lock);
}

static void log_callback(enum ggml_log_level level, const char *text, void *user_data) {
    (void) user_data;
    if (text == NULL) {
        return;
    }
    int prio;
    switch (level) {
        case GGML_LOG_LEVEL_ERROR: prio = ANDROID_LOG_ERROR; break;
        case GGML_LOG_LEVEL_WARN:  prio = ANDROID_LOG_WARN;  break;
        case GGML_LOG_LEVEL_INFO:  prio = ANDROID_LOG_INFO;  break;
        case GGML_LOG_LEVEL_DEBUG: prio = ANDROID_LOG_DEBUG; break;
        default:                   prio = ANDROID_LOG_INFO;  break;
    }
    __android_log_write(prio, "llama.cpp", text);
    if (level == GGML_LOG_LEVEL_ERROR) {
        pthread_mutex_lock(&g_err_lock);
        snprintf(g_last_engine_log, sizeof g_last_engine_log, "%s", text);
        pthread_mutex_unlock(&g_err_lock);
    }
}

static void ensure_backend(void) {
    if (atomic_exchange(&g_backend_ready, 1) == 0) {
        llama_log_set(log_callback, NULL);
        llama_backend_init();
    }
}

static bool abort_callback(void *data) {
    struct engine *e = (struct engine *) data;
    return e != NULL && atomic_load(&e->cancel) != 0;
}

static const char *process_status(int32_t rc) {
    switch (rc) {
        case 0:  return "ok";
        case 1:  return "no memory slot for the batch (context full)";
        case 2:  return "aborted (cancelled)";
        case -1: return "invalid input batch";
        default: return "fatal error";
    }
}

static long rss_kib(void) {
    FILE *f = fopen("/proc/self/status", "r");
    if (f == NULL) {
        return -1;
    }
    char line[256];
    long kib = -1;
    while (fgets(line, sizeof line, f) != NULL) {
        if (strncmp(line, "VmRSS:", 6) == 0) {
            kib = strtol(line + 6, NULL, 10);
            break;
        }
    }
    fclose(f);
    return kib;
}

/* ------------------------------------------------------------------ JNI: info */

JNIEXPORT jbyteArray JNICALL
Java_org_llamaengine_selftest_LlamaEngine_info(JNIEnv *env, jclass clazz) {
    (void) clazz;
    ensure_backend();
    struct textbuf tb;
    tb_init(&tb);
    tb_printf(&tb, "bridge: %s\n", BRIDGE_VERSION);
    tb_printf(&tb, "llama.cpp version: %s\n", llama_version());
    tb_printf(&tb, "ggml version: %s, commit %s\n", ggml_version(), ggml_commit());
    tb_printf(&tb, "memory page size: %ld bytes\n", sysconf(_SC_PAGESIZE));
    tb_printf(&tb, "CPUs online: %ld\n", sysconf(_SC_NPROCESSORS_ONLN));
    tb_printf(&tb, "mmap supported: %s, mlock supported: %s\n",
              llama_supports_mmap() ? "yes" : "no", llama_supports_mlock() ? "yes" : "no");
    tb_printf(&tb, "runtime CPU features: NEON=%d ARM_FMA=%d FP16_VA=%d DOTPROD=%d I8MM=%d SVE=%d SME=%d\n",
              ggml_cpu_has_neon(), ggml_cpu_has_arm_fma(), ggml_cpu_has_fp16_va(),
              ggml_cpu_has_dotprod(), ggml_cpu_has_matmul_int8(), ggml_cpu_has_sve(), ggml_cpu_has_sme());
    size_t ndev = ggml_backend_dev_count();
    tb_printf(&tb, "ggml devices: %zu\n", ndev);
    for (size_t i = 0; i < ndev; i++) {
        ggml_backend_dev_t dev = ggml_backend_dev_get(i);
        tb_printf(&tb, "  device %zu: %s (%s)\n", i, ggml_backend_dev_name(dev), ggml_backend_dev_description(dev));
    }
    tb_printf(&tb, "llama_print_system_info: %s\n", llama_print_system_info());
    tb_printf(&tb, "process RSS: %ld KiB\n", rss_kib());
    tb_printf(&tb, "live engine handles: %d\n", atomic_load(&g_live_handles));
    jbyteArray out = tb_to_bytes(env, &tb);
    tb_free(&tb);
    return out;
}

/* ------------------------------------------------------------------ JNI: load */

JNIEXPORT jlong JNICALL
Java_org_llamaengine_selftest_LlamaEngine_load(JNIEnv *env, jclass clazz, jstring jpath, jint n_threads, jint n_ctx) {
    (void) clazz;
    ensure_backend();
    if (jpath == NULL) {
        set_error("load: model path is null");
        return 0;
    }
    if (n_threads < 1 || n_ctx < 64) {
        set_error("load: n_threads must be >= 1 and n_ctx >= 64");
        return 0;
    }
    clear_error();
    const char *path = (*env)->GetStringUTFChars(env, jpath, NULL);
    if (path == NULL) {
        set_error("load: could not read the model path");
        return 0;
    }

    struct engine *e = (struct engine *) calloc(1, sizeof *e);
    if (e == NULL) {
        (*env)->ReleaseStringUTFChars(env, jpath, path);
        set_error("load: out of memory");
        return 0;
    }
    atomic_init(&e->cancel, 0);
    e->n_threads = (int32_t) n_threads;
    e->n_ctx = (uint32_t) n_ctx;

    struct llama_model_params mparams = llama_model_default_params();
    mparams.n_gpu_layers = 0; /* CPU only */
    e->model = llama_model_load_from_file(path, mparams);
    (*env)->ReleaseStringUTFChars(env, jpath, path);
    if (e->model == NULL) {
        free(e);
        set_error("load: llama_model_load_from_file failed (see logcat tag llama.cpp)");
        return 0;
    }
    e->vocab = llama_model_get_vocab(e->model);

    struct llama_context_params cparams = llama_context_default_params();
    cparams.n_ctx = (uint32_t) n_ctx;
    cparams.n_batch = (uint32_t) (n_ctx < 512 ? n_ctx : 512);
    cparams.n_ubatch = cparams.n_batch;
    cparams.n_threads = (int32_t) n_threads;
    cparams.n_threads_batch = (int32_t) n_threads;
    cparams.no_perf = false;
    cparams.abort_callback = abort_callback;
    cparams.abort_callback_data = e;
    e->ctx = llama_init_from_model(e->model, cparams);
    if (e->ctx == NULL) {
        llama_model_free(e->model);
        free(e);
        set_error("load: llama_init_from_model failed (context too large for memory?)");
        return 0;
    }

    struct llama_sampler_chain_params sparams = llama_sampler_chain_default_params();
    sparams.no_perf = false;
    e->smpl = llama_sampler_chain_init(sparams);
    if (e->smpl == NULL) {
        llama_free(e->ctx);
        llama_model_free(e->model);
        free(e);
        set_error("load: llama_sampler_chain_init failed");
        return 0;
    }
    llama_sampler_chain_add(e->smpl, llama_sampler_init_greedy());

    atomic_fetch_add(&g_live_handles, 1);
    return (jlong) (intptr_t) e;
}

/* ------------------------------------------------------------------ JNI: describe */

JNIEXPORT jbyteArray JNICALL
Java_org_llamaengine_selftest_LlamaEngine_describe(JNIEnv *env, jclass clazz, jlong handle) {
    (void) clazz;
    struct engine *e = (struct engine *) (intptr_t) handle;
    struct textbuf tb;
    tb_init(&tb);
    if (e == NULL) {
        tb_printf(&tb, "no model loaded\n");
    } else {
        char desc[256];
        if (llama_model_desc(e->model, desc, sizeof desc) < 0) {
            snprintf(desc, sizeof desc, "(no description)");
        }
        tb_printf(&tb, "model: %s\n", desc);
        tb_printf(&tb, "parameters: %llu, tensor bytes: %llu\n",
                  (unsigned long long) llama_model_n_params(e->model),
                  (unsigned long long) llama_model_size(e->model));
        tb_printf(&tb, "training context: %d, vocabulary: %d tokens\n",
                  llama_model_n_ctx_train(e->model), llama_vocab_n_tokens(e->vocab));
        tb_printf(&tb, "context: %u tokens, batch: %u, threads: %d\n",
                  llama_n_ctx(e->ctx), llama_n_batch(e->ctx), e->n_threads);
        tb_printf(&tb, "process RSS: %ld KiB\n", rss_kib());
    }
    jbyteArray out = tb_to_bytes(env, &tb);
    tb_free(&tb);
    return out;
}

/* ------------------------------------------------------------------ JNI: generate */

static int32_t decode_tokens(struct engine *e, struct llama_batch_ext *batch,
                             const llama_token *tokens, int32_t n, llama_pos pos0, bool logits_last) {
    llama_batch_ext_clear(batch);
    for (int32_t i = 0; i < n; i++) {
        int32_t idx = llama_batch_ext_add_token(batch, 0, tokens[i]);
        if (idx < 0) {
            return -1;
        }
        llama_pos pos = pos0 + i;
        llama_batch_ext_set_pos(batch, idx, &pos);
    }
    if (logits_last && n > 0) {
        llama_batch_ext_set_output_logits(batch, n - 1, true);
    }
    return llama_process(e->ctx, LLAMA_PROCESS_TYPE_DECODE, batch);
}

JNIEXPORT jbyteArray JNICALL
Java_org_llamaengine_selftest_LlamaEngine_generate(JNIEnv *env, jclass clazz, jlong handle, jstring jprompt, jint max_tokens) {
    (void) clazz;
    struct engine *e = (struct engine *) (intptr_t) handle;
    struct textbuf tb;
    tb_init(&tb);

    if (e == NULL || jprompt == NULL || max_tokens < 1) {
        tb_printf(&tb, "RESULT: ERROR\nreason: no model loaded, no prompt, or max_tokens < 1\n");
        jbyteArray out = tb_to_bytes(env, &tb);
        tb_free(&tb);
        return out;
    }
    atomic_store(&e->cancel, 0);

    const char *prompt = (*env)->GetStringUTFChars(env, jprompt, NULL);
    if (prompt == NULL) {
        tb_printf(&tb, "RESULT: ERROR\nreason: could not read the prompt\n");
        jbyteArray out = tb_to_bytes(env, &tb);
        tb_free(&tb);
        return out;
    }
    int32_t prompt_len = (int32_t) strlen(prompt);

    /* start every run from an empty context */
    llama_memory_clear(llama_get_memory(e->ctx), true);
    llama_sampler_reset(e->smpl);

    int32_t n_prompt = -llama_tokenize(e->vocab, prompt, prompt_len, NULL, 0, true, true);
    llama_token *tokens = NULL;
    if (n_prompt > 0) {
        tokens = (llama_token *) malloc((size_t) n_prompt * sizeof *tokens);
    }
    if (tokens == NULL || llama_tokenize(e->vocab, prompt, prompt_len, tokens, n_prompt, true, true) < 0) {
        (*env)->ReleaseStringUTFChars(env, jprompt, prompt);
        free(tokens);
        tb_printf(&tb, "RESULT: ERROR\nreason: tokenization failed (prompt tokens: %d)\n", n_prompt);
        jbyteArray out = tb_to_bytes(env, &tb);
        tb_free(&tb);
        return out;
    }
    (*env)->ReleaseStringUTFChars(env, jprompt, prompt);

    uint32_t n_ctx = llama_n_ctx(e->ctx);
    int32_t n_predict = (int32_t) max_tokens;
    if ((uint32_t) n_prompt >= n_ctx) {
        free(tokens);
        tb_printf(&tb, "RESULT: ERROR\nreason: prompt has %d tokens, context is %u\n", n_prompt, n_ctx);
        jbyteArray out = tb_to_bytes(env, &tb);
        tb_free(&tb);
        return out;
    }
    if ((uint32_t) (n_prompt + n_predict) > n_ctx) {
        n_predict = (int32_t) n_ctx - n_prompt;
    }

    struct llama_batch_ext *batch = llama_batch_ext_init(e->ctx);
    if (batch == NULL) {
        free(tokens);
        tb_printf(&tb, "RESULT: ERROR\nreason: llama_batch_ext_init failed\n");
        jbyteArray out = tb_to_bytes(env, &tb);
        tb_free(&tb);
        return out;
    }
    int32_t n_batch = (int32_t) llama_n_batch(e->ctx);

    struct textbuf text;
    tb_init(&text);
    const char *stop = "max_tokens reached";
    int32_t rc = 0;

    /* prompt processing, in chunks of n_batch tokens */
    int64_t t0 = ggml_time_us();
    for (int32_t done = 0; done < n_prompt; done += n_batch) {
        int32_t n = n_prompt - done < n_batch ? n_prompt - done : n_batch;
        bool last = done + n >= n_prompt;
        rc = decode_tokens(e, batch, tokens + done, n, (llama_pos) done, last);
        if (rc != 0) {
            break;
        }
    }
    int64_t t1 = ggml_time_us();

    int32_t n_gen = 0;
    llama_pos n_pos = (llama_pos) n_prompt;
    if (rc == 0) {
        for (; n_gen < n_predict; ) {
            if (atomic_load(&e->cancel)) {
                stop = "cancelled";
                break;
            }
            llama_token id = llama_sampler_sample(e->smpl, e->ctx, -1);
            if (llama_vocab_is_eog(e->vocab, id)) {
                stop = "end of generation token";
                break;
            }
            char piece[256];
            int32_t np = llama_token_to_piece(e->vocab, id, piece, (int32_t) sizeof piece, 0, true);
            if (np < 0) {
                char *big = (char *) malloc((size_t) (-np));
                if (big != NULL) {
                    int32_t np2 = llama_token_to_piece(e->vocab, id, big, -np, 0, true);
                    if (np2 > 0) {
                        tb_append(&text, big, (size_t) np2);
                    }
                    free(big);
                }
            } else if (np > 0) {
                tb_append(&text, piece, (size_t) np);
            }
            n_gen++;
            if (n_gen >= n_predict) {
                break;
            }
            rc = decode_tokens(e, batch, &id, 1, n_pos, true);
            n_pos++;
            if (rc != 0) {
                break;
            }
        }
    }
    int64_t t2 = ggml_time_us();
    llama_batch_ext_free(batch);
    free(tokens);

    double pp_s = (double) (t1 - t0) / 1e6;
    double tg_s = (double) (t2 - t1) / 1e6;
    if (rc == 2 || atomic_load(&e->cancel)) {
        stop = "cancelled";
    } else if (rc != 0) {
        stop = process_status(rc);
    }
    tb_printf(&tb, "RESULT: %s\n", (rc == 0 || rc == 2 || atomic_load(&e->cancel)) ? "OK" : "ERROR");
    tb_printf(&tb, "stop: %s (llama_process rc=%d: %s)\n", stop, rc, process_status(rc));
    tb_printf(&tb, "prompt: %d tokens in %.3f s (%.2f tokens/s)\n", n_prompt, pp_s, pp_s > 0 ? n_prompt / pp_s : 0.0);
    tb_printf(&tb, "generated: %d tokens in %.3f s (%.2f tokens/s)\n", n_gen, tg_s, tg_s > 0 ? n_gen / tg_s : 0.0);
    tb_printf(&tb, "threads: %d, context: %u, process RSS: %ld KiB\n", e->n_threads, n_ctx, rss_kib());
    tb_printf(&tb, "--- generated text (greedy) ---\n");
    if (text.data != NULL) {
        tb_append(&tb, text.data, text.len);
    }
    tb_printf(&tb, "\n--- end ---\n");
    tb_free(&text);

    jbyteArray out = tb_to_bytes(env, &tb);
    tb_free(&tb);
    return out;
}

/* ------------------------------------------------------------------ JNI: cancel, release, misc */

JNIEXPORT void JNICALL
Java_org_llamaengine_selftest_LlamaEngine_cancel(JNIEnv *env, jclass clazz, jlong handle) {
    (void) env;
    (void) clazz;
    struct engine *e = (struct engine *) (intptr_t) handle;
    if (e != NULL) {
        atomic_store(&e->cancel, 1);
    }
}

JNIEXPORT void JNICALL
Java_org_llamaengine_selftest_LlamaEngine_release(JNIEnv *env, jclass clazz, jlong handle) {
    (void) env;
    (void) clazz;
    struct engine *e = (struct engine *) (intptr_t) handle;
    if (e == NULL) {
        return;
    }
    if (e->smpl != NULL) {
        llama_sampler_free(e->smpl);
    }
    if (e->ctx != NULL) {
        llama_free(e->ctx);
    }
    if (e->model != NULL) {
        llama_model_free(e->model);
    }
    free(e);
    atomic_fetch_sub(&g_live_handles, 1);
}

JNIEXPORT jint JNICALL
Java_org_llamaengine_selftest_LlamaEngine_liveHandles(JNIEnv *env, jclass clazz) {
    (void) env;
    (void) clazz;
    return (jint) atomic_load(&g_live_handles);
}

JNIEXPORT jbyteArray JNICALL
Java_org_llamaengine_selftest_LlamaEngine_lastError(JNIEnv *env, jclass clazz) {
    (void) clazz;
    struct textbuf tb;
    tb_init(&tb);
    pthread_mutex_lock(&g_err_lock);
    tb_printf(&tb, "%s", g_last_error);
    if (g_last_engine_log[0] != '\0') {
        tb_printf(&tb, "\nlast llama.cpp error: %s", g_last_engine_log);
    }
    pthread_mutex_unlock(&g_err_lock);
    jbyteArray out = tb_to_bytes(env, &tb);
    tb_free(&tb);
    return out;
}
