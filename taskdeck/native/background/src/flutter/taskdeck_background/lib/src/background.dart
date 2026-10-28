import 'package:flet/flet.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

/// Passes start/update/stop from Python to the Android foreground service (BackgroundPlugin.java).
class BackgroundWorkService extends FletService {
  BackgroundWorkService({required super.control});

  static const _channel = MethodChannel("dev.taskdeck.background");

  @override
  void init() {
    super.init();
    control.addInvokeMethodListener(_invokeMethod);
  }

  Future<dynamic> _invokeMethod(String name, dynamic args) async {
    if (defaultTargetPlatform != TargetPlatform.android) {
      return null;
    }
    switch (name) {
      case "start":
      case "update":
      case "stop":
        return await _channel.invokeMethod(name, args);
      default:
        throw Exception("Unknown BackgroundWork method: $name");
    }
  }

  @override
  void dispose() {
    control.removeInvokeMethodListener(_invokeMethod);
    super.dispose();
  }
}
