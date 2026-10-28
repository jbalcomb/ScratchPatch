import 'package:flet/flet.dart';

import 'background.dart';

class Extension extends FletExtension {
  @override
  FletService? createService(Control control) {
    switch (control.type) {
      case "BackgroundWork":
        return BackgroundWorkService(control: control);
      default:
        return null;
    }
  }
}
