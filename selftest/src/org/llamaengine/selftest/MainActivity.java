package org.llamaengine.selftest;

import android.app.Activity;
import android.content.ContentResolver;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.OpenableColumns;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * llama.cpp engine self-test (APK Builder 004, profile cpp-native-v1 with
 * native.prebuilt). It only validates the native engine on the phone:
 * library loading, CPU features and page size, copying a user-chosen GGUF
 * model into app storage, loading it, greedy generation on a background
 * thread with cancel, release, and repeated load/release cycles.
 * It is not a chat application.
 */
public class MainActivity extends Activity {
    private static final int PICK_MODEL = 1;
    private static final String PROMPT = "The capital of France is";
    private static final int MAX_TOKENS = 64;
    private static final int N_CTX = 1024;
    private static final int CYCLES = 5;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final StringBuilder log = new StringBuilder();
    private TextView out;
    private volatile long handle;
    private volatile boolean busy;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(32, 32, 32, 32);
        button(root, "1. Engine info", this::engineInfo);
        button(root, "2. Choose a GGUF model file", this::chooseModel);
        button(root, "3. Load model (CPU, " + threads() + " threads)", this::loadModel);
        button(root, "4. Generate " + MAX_TOKENS + " tokens (background)", this::generate);
        button(root, "5. Cancel generation", this::cancelRun);
        button(root, "6. Release model", this::releaseModel);
        button(root, "7. Load and release " + CYCLES + " times", this::cycles);
        button(root, "Clear log", this::clearLog);
        out = new TextView(this);
        out.setTextSize(13);
        out.setTextIsSelectable(true);
        root.addView(out);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(root);
        setContentView(scroll);

        String err = LlamaEngine.loadError();
        if (err != null) {
            say("ENGINE-LOADED: NO\n" + err);
        } else {
            say("ENGINE-LOADED: yes (libllamabridge.so and its dependencies)");
            File model = modelFile();
            say(model.exists() ? "model file present: " + model.length() + " bytes" : "no model copied yet: use button 2");
        }
    }

    private static int threads() {
        int n = Runtime.getRuntime().availableProcessors();
        return Math.max(1, Math.min(4, n));
    }

    private File modelFile() {
        return new File(getFilesDir(), "model.gguf");
    }

    private void button(LinearLayout root, String text, Runnable action) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setOnClickListener(v -> action.run());
        root.addView(b);
    }

    private void say(String s) {
        ui.post(() -> {
            log.append(s).append('\n');
            out.setText(log);
        });
    }

    private void clearLog() {
        ui.post(() -> {
            log.setLength(0);
            out.setText("");
        });
    }

    private boolean engineMissing() {
        if (LlamaEngine.loadError() != null) {
            say("not available: the engine libraries did not load");
            return true;
        }
        return false;
    }

    /** Runs a task on the single worker thread, one at a time. */
    private void background(String name, Runnable task) {
        if (busy) {
            say("busy: wait for the current task, or use Cancel");
            return;
        }
        busy = true;
        worker.execute(() -> {
            long t0 = SystemClock.elapsedRealtime();
            try {
                task.run();
            } catch (Throwable t) {
                say(name + " FAILED: " + t);
            } finally {
                busy = false;
                say(name + " finished in " + (SystemClock.elapsedRealtime() - t0) + " ms");
            }
        });
    }

    private void engineInfo() {
        if (engineMissing()) {
            return;
        }
        background("engine info", () -> say("--- engine info ---\n" + LlamaEngine.utf8(LlamaEngine.info())));
    }

    private void chooseModel() {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("*/*");
        try {
            startActivityForResult(i, PICK_MODEL);
        } catch (Throwable t) {
            say("cannot open the file picker: " + t);
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != PICK_MODEL || resultCode != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        final Uri uri = data.getData();
        if (handle != 0) {
            say("release the loaded model (button 6) before copying a new one");
            return;
        }
        background("copy model", () -> copyModel(uri));
    }

    private void copyModel(Uri uri) {
        ContentResolver cr = getContentResolver();
        long size = -1;
        String name = "(unknown)";
        Cursor c = null;
        try {
            c = cr.query(uri, null, null, null, null);
            if (c != null && c.moveToFirst()) {
                int si = c.getColumnIndex(OpenableColumns.SIZE);
                int ni = c.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (si >= 0 && !c.isNull(si)) {
                    size = c.getLong(si);
                }
                if (ni >= 0) {
                    name = c.getString(ni);
                }
            }
        } finally {
            if (c != null) {
                c.close();
            }
        }
        File dst = modelFile();
        File tmp = new File(getFilesDir(), "model.gguf.part");
        long free = getFilesDir().getUsableSpace() + (dst.exists() ? dst.length() : 0);
        say("chosen: " + name + ", size " + (size < 0 ? "unknown" : size + " bytes") + ", free space " + free + " bytes");
        if (size > 0 && size + 64L * 1024 * 1024 > free) {
            say("NOT COPIED: not enough free space (need the model size plus 64 MiB)");
            return;
        }
        long t0 = SystemClock.elapsedRealtime();
        long copied = 0;
        byte[] buf = new byte[1 << 20];
        boolean gguf = false;
        try (InputStream in = cr.openInputStream(uri); OutputStream os = new FileOutputStream(tmp)) {
            if (in == null) {
                say("NOT COPIED: the file could not be opened");
                return;
            }
            int n;
            long nextReport = 256L * 1024 * 1024;
            while ((n = in.read(buf)) > 0) {
                if (copied == 0 && n >= 4) {
                    gguf = buf[0] == 'G' && buf[1] == 'G' && buf[2] == 'U' && buf[3] == 'F';
                }
                os.write(buf, 0, n);
                copied += n;
                if (copied >= nextReport) {
                    say("copied " + (copied >> 20) + " MiB ...");
                    nextReport += 256L * 1024 * 1024;
                }
            }
        } catch (Throwable t) {
            tmp.delete();
            say("NOT COPIED: " + t);
            return;
        }
        if (!gguf) {
            tmp.delete();
            say("NOT COPIED: the file does not start with the GGUF magic bytes");
            return;
        }
        if (dst.exists() && !dst.delete()) {
            tmp.delete();
            say("NOT COPIED: could not replace the previous model file");
            return;
        }
        if (!tmp.renameTo(dst)) {
            tmp.delete();
            say("NOT COPIED: could not rename the copied file");
            return;
        }
        long ms = SystemClock.elapsedRealtime() - t0;
        say("model copied: " + copied + " bytes in " + ms + " ms -> " + dst.getAbsolutePath());
    }

    private void loadModel() {
        if (engineMissing()) {
            return;
        }
        if (handle != 0) {
            say("a model is already loaded");
            return;
        }
        final File model = modelFile();
        if (!model.exists()) {
            say("no model file: use button 2 first");
            return;
        }
        background("load model", () -> {
            long t0 = SystemClock.elapsedRealtime();
            long h = LlamaEngine.load(model.getAbsolutePath(), threads(), N_CTX);
            long ms = SystemClock.elapsedRealtime() - t0;
            if (h == 0) {
                say("LOAD FAILED after " + ms + " ms: " + LlamaEngine.utf8(LlamaEngine.lastError()));
                return;
            }
            handle = h;
            say("LOADED in " + ms + " ms\n" + LlamaEngine.utf8(LlamaEngine.describe(h)));
        });
    }

    private void generate() {
        if (engineMissing()) {
            return;
        }
        final long h = handle;
        if (h == 0) {
            say("load a model first (button 3)");
            return;
        }
        say("prompt: \"" + PROMPT + "\"");
        background("generate", () -> say(LlamaEngine.utf8(LlamaEngine.generate(h, PROMPT, MAX_TOKENS))));
    }

    private void cancelRun() {
        long h = handle;
        if (h != 0 && busy) {
            LlamaEngine.cancel(h);
            say("cancel requested");
        } else {
            say("nothing to cancel");
        }
    }

    private void releaseModel() {
        if (engineMissing()) {
            return;
        }
        final long h = handle;
        if (h == 0) {
            say("no model loaded");
            return;
        }
        background("release model", () -> {
            handle = 0;
            LlamaEngine.release(h);
            say("released; live handles: " + LlamaEngine.liveHandles() + "\n" + LlamaEngine.utf8(LlamaEngine.info()));
        });
    }

    private void cycles() {
        if (engineMissing()) {
            return;
        }
        if (handle != 0) {
            say("release the loaded model first (button 6)");
            return;
        }
        final File model = modelFile();
        if (!model.exists()) {
            say("no model file: use button 2 first");
            return;
        }
        background("load/release cycles", () -> {
            for (int i = 1; i <= CYCLES; i++) {
                long t0 = SystemClock.elapsedRealtime();
                long h = LlamaEngine.load(model.getAbsolutePath(), threads(), N_CTX);
                if (h == 0) {
                    say("cycle " + i + ": LOAD FAILED: " + LlamaEngine.utf8(LlamaEngine.lastError()));
                    return;
                }
                LlamaEngine.release(h);
                say("cycle " + i + ": load+release " + (SystemClock.elapsedRealtime() - t0)
                        + " ms, live handles " + LlamaEngine.liveHandles());
            }
            say("CYCLES DONE: " + CYCLES + " of " + CYCLES);
        });
    }

    @Override
    protected void onDestroy() {
        final long h = handle;
        if (h != 0) {
            LlamaEngine.cancel(h);
            handle = 0;
            worker.execute(() -> LlamaEngine.release(h));
        }
        worker.shutdown();
        super.onDestroy();
    }
}
