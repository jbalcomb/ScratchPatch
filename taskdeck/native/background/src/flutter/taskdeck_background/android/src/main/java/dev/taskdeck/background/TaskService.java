package dev.taskdeck.background;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

/**
 * Foreground service that keeps TaskDeck's process running at full priority while a task runs
 * (Android otherwise cuts the network and CPU of apps in the background). The work itself stays
 * in TaskDeck's Python; this only shows the ongoing notification and holds wake/Wi-Fi locks.
 */
public class TaskService extends Service {
    private static final String TAG = "TaskDeckBackground";
    private static final String CHANNEL_RUNNING = "taskdeck_running";
    private static final String CHANNEL_FINISHED = "taskdeck_finished";
    private static final int ID_RUNNING = 4101;
    private static final int ID_FINISHED = 4102;
    private static final String EXTRA_TITLE = "title";
    private static final String EXTRA_TEXT = "text";
    private static final String EXTRA_PROGRESS = "progress";

    private static volatile boolean running = false;

    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;

    // ---- called from BackgroundPlugin ----------------------------------------------------------

    static boolean start(Context context, String title, String text, int progress) {
        Intent intent = new Intent(context, TaskService.class)
                .putExtra(EXTRA_TITLE, title).putExtra(EXTRA_TEXT, text).putExtra(EXTRA_PROGRESS, progress);
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(intent);
            } else {
                context.startService(intent);
            }
            return true;
        } catch (Exception e) {  // e.g. not allowed to start from the background (Android 12+)
            Log.w(TAG, "Can't start the foreground service", e);
            return false;
        }
    }

    static void update(Context context, String title, String text, int progress) {
        if (running) {
            manager(context).notify(ID_RUNNING, build(context, title, text, progress));
        }
    }

    /** Stops the service; with a title, leaves a "finished" notification behind. */
    static void stop(Context context, String title, String text) {
        context.stopService(new Intent(context, TaskService.class));
        if (title != null && !title.isEmpty()) {
            channels(context);
            Notification.Builder b = builder(context, CHANNEL_FINISHED)
                    .setContentTitle(title).setAutoCancel(true)
                    .setSmallIcon(android.R.drawable.stat_sys_download_done);
            if (text != null) {
                b.setContentText(text).setStyle(new Notification.BigTextStyle().bigText(text));
            }
            manager(context).notify(ID_FINISHED, b.build());
        }
    }

    // ---- the service --------------------------------------------------------------------------------

    @Override
    public void onCreate() {
        super.onCreate();
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "TaskDeck:task");
        wakeLock.acquire(6 * 60 * 60 * 1000L);  // safety cap; released in onDestroy
        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        if (wm != null) {
            wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "TaskDeck:task");
            wifiLock.acquire();
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String title = intent != null ? intent.getStringExtra(EXTRA_TITLE) : null;
        String text = intent != null ? intent.getStringExtra(EXTRA_TEXT) : null;
        int progress = intent != null ? intent.getIntExtra(EXTRA_PROGRESS, -1) : -1;
        Notification n = build(this, title, text, progress);
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(ID_RUNNING, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
            } else {
                startForeground(ID_RUNNING, n);
            }
            running = true;
        } catch (Exception e) {
            Log.w(TAG, "startForeground failed", e);
            stopSelf();
        }
        return START_NOT_STICKY;  // the work lives in the app process; nothing to redo after a kill
    }

    @Override
    public void onTimeout(int startId, int fgsType) {  // Android 15: data-sync services get ~6 hours a day
        stopSelf();
    }

    @Override
    public void onDestroy() {
        running = false;
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        if (wifiLock != null && wifiLock.isHeld()) {
            wifiLock.release();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ---- notifications ------------------------------------------------------------------------------

    private static Notification build(Context context, String title, String text, int progress) {
        channels(context);
        Notification.Builder b = builder(context, CHANNEL_RUNNING)
                .setContentTitle(title != null ? title : "TaskDeck")
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setProgress(100, Math.max(0, progress), progress < 0);
        if (text != null) {
            b.setContentText(text);
        }
        if (Build.VERSION.SDK_INT >= 31) {
            b.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
        }
        return b.build();
    }

    private static Notification.Builder builder(Context context, String channel) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(context, channel)
                : new Notification.Builder(context);
        Intent open = context.getPackageManager().getLaunchIntentForPackage(context.getPackageName());
        if (open != null) {
            open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
            b.setContentIntent(PendingIntent.getActivity(context, 0, open,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
        }
        return b;
    }

    private static void channels(Context context) {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager m = manager(context);
            m.createNotificationChannel(new NotificationChannel(
                    CHANNEL_RUNNING, "Running tasks", NotificationManager.IMPORTANCE_LOW));
            m.createNotificationChannel(new NotificationChannel(
                    CHANNEL_FINISHED, "Finished tasks", NotificationManager.IMPORTANCE_DEFAULT));
        }
    }

    private static NotificationManager manager(Context context) {
        return (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
    }
}
