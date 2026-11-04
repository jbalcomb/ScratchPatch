package dev.taskdeck.background;

import android.content.Context;
import android.os.Build;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

import io.flutter.embedding.engine.plugins.FlutterPlugin;
import io.flutter.plugin.common.MethodCall;
import io.flutter.plugin.common.MethodChannel;

/**
 * "dev.taskdeck.background" channel: start / update / stop the foreground service (TaskService),
 * and last_crash: the stack trace of the Java/Kotlin exception that last closed TaskDeck (any plugin's),
 * saved by the handler installed here, so the app can show it on the next start.
 */
public class BackgroundPlugin implements FlutterPlugin, MethodChannel.MethodCallHandler {
    private static final String CRASH_FILE = "taskdeck-crash.txt";
    private static boolean crashHandlerInstalled = false;
    private MethodChannel channel;
    private Context context;

    private static synchronized void installCrashHandler(Context context) {
        if (crashHandlerInstalled) {
            return;
        }
        crashHandlerInstalled = true;
        final File file = new File(context.getFilesDir(), CRASH_FILE);
        final Thread.UncaughtExceptionHandler previous = Thread.getDefaultUncaughtExceptionHandler();
        Thread.setDefaultUncaughtExceptionHandler((thread, error) -> {
            try {
                StringWriter trace = new StringWriter();
                error.printStackTrace(new PrintWriter(trace));
                String when = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss Z", Locale.US).format(new Date());
                String text = "When: " + when + "\nThread: " + thread.getName() + "\nAndroid: "
                        + Build.VERSION.RELEASE + " (SDK " + Build.VERSION.SDK_INT + "), " + Build.MANUFACTURER
                        + " " + Build.MODEL + "\n\n" + trace;
                try (FileOutputStream out = new FileOutputStream(file)) {
                    out.write(text.getBytes(StandardCharsets.UTF_8));
                }
            } catch (Throwable ignored) {
                // never get in the way of the crash itself
            }
            if (previous != null) {
                previous.uncaughtException(thread, error);
            }
        });
    }

    private String takeLastCrash() throws Exception {
        File file = new File(context.getFilesDir(), CRASH_FILE);
        if (!file.exists()) {
            return null;
        }
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (FileInputStream in = new FileInputStream(file)) {
            byte[] buffer = new byte[8192];
            for (int n; (n = in.read(buffer)) > 0; ) {
                bytes.write(buffer, 0, n);
            }
        }
        file.delete();
        return new String(bytes.toByteArray(), StandardCharsets.UTF_8);
    }

    @Override
    public void onAttachedToEngine(FlutterPluginBinding binding) {
        context = binding.getApplicationContext();
        installCrashHandler(context);
        channel = new MethodChannel(binding.getBinaryMessenger(), "dev.taskdeck.background");
        channel.setMethodCallHandler(this);
    }

    @Override
    public void onDetachedFromEngine(FlutterPluginBinding binding) {
        channel.setMethodCallHandler(null);
        channel = null;
    }

    @Override
    public void onMethodCall(MethodCall call, MethodChannel.Result result) {
        String title = call.argument("title");
        String text = call.argument("text");
        Integer progress = call.argument("progress");  // 0-100, or null/-1 for "busy"
        int pct = progress == null ? -1 : progress;
        try {
            switch (call.method) {
                case "start":
                    result.success(TaskService.start(context, title, text, pct));
                    break;
                case "update":
                    TaskService.update(context, title, text, pct);
                    result.success(true);
                    break;
                case "stop":
                    TaskService.stop(context, title, text);
                    result.success(true);
                    break;
                case "last_crash":
                    result.success(takeLastCrash());
                    break;
                default:
                    result.notImplemented();
            }
        } catch (Exception e) {
            result.error("background", e.toString(), null);
        }
    }
}
