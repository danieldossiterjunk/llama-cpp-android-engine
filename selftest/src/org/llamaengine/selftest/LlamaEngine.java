package org.llamaengine.selftest;

import java.nio.charset.StandardCharsets;

/**
 * Java side of jni/llamabridge.c. Loads only the bridge; Android loads the
 * engine libraries it needs (libllama.so, libggml.so, libggml-base.so,
 * libggml-cpu.so) from the APK by itself, dependencies first.
 *
 * Every native method returns plain values or UTF-8 bytes. All of them except
 * cancel() must be called from one background thread, never the UI thread.
 */
public final class LlamaEngine {
    private static final String LOAD_ERROR;

    static {
        String err = null;
        try {
            System.loadLibrary("llamabridge");
        } catch (Throwable t) {
            err = t.toString();
        }
        LOAD_ERROR = err;
    }

    private LlamaEngine() {
    }

    /** null when the libraries loaded, otherwise the error Android reported. */
    public static String loadError() {
        return LOAD_ERROR;
    }

    static String utf8(byte[] bytes) {
        return bytes == null ? "(null)" : new String(bytes, StandardCharsets.UTF_8);
    }

    static native byte[] info();

    static native long load(String modelPath, int nThreads, int nCtx);

    static native byte[] describe(long handle);

    static native byte[] generate(long handle, String prompt, int maxTokens);

    static native void cancel(long handle);

    static native void release(long handle);

    static native int liveHandles();

    static native byte[] lastError();
}
