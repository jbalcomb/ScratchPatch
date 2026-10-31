static op_read_bytes(out, ea, name, size) {
  auto i, b, line, hex, text, n;
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  n = 0;
  for (line = ea; line < ea + size; line = line + 16) {
    hex = "";
    text = "";
    for (i = line; i < line + 16 && i < ea + size; i++) {
      b = Byte(i);
      if (hex != "") hex = hex + " ";
      hex = hex + form("%02X", b);
      if (b >= 0x20 && b < 0x7F && b != 0x5C) text = text + form("%c", b);
      else text = text + ".";
    }
    fprintf(out, "%s\t%s\t%s\n", hx(line), hex, text);
    n = n + 1;
  }
  total(out, n);
  return 0;
}
