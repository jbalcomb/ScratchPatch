package dev.taskdeck.background;

import android.content.Context;

import io.flutter.embedding.engine.plugins.FlutterPlugin;
import io.flutter.plugin.common.MethodCall;
import io.flutter.plugin.common.MethodChannel;

/** "dev.taskdeck.background" channel: start / update / stop the foreground service (TaskService). */
public class BackgroundPlugin implements FlutterPlugin, MethodChannel.MethodCallHandler {
    private MethodChannel channel;
    private Context context;

    @Override
    public void onAttachedToEngine(FlutterPluginBinding binding) {
        context = binding.getApplicationContext();
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
                default:
                    result.notImplemented();
            }
        } catch (Exception e) {
            result.error("background", e.toString(), null);
        }
    }
}
