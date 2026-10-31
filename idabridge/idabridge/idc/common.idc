// Helpers shared by every operation. Written for the IDC of IDA Pro 5.5:
// no initialisers on `auto`, no sprintf (use form), and Exit() rather than qexit().
// Each operation lives in its own file under ops/ so an IDC function that IDA 5.5 lacks
// breaks only that operation, not all of them.
//
// Output protocol (one line each, fields separated by tabs, \t \n \r \\ escaped):
//   #INFO <key> <value>     a named value
//   #ERROR <message>        the operation failed; it then returns 1
//   #TOTAL <n>              rows that matched before paging
//   <field>\t<field>...     a data row
//   #END <rc>               written by the generated main(): the script ran to completion

static esc(s) {
  auto i, n, c, r;
  if (strstr(s, "\t") == -1 && strstr(s, "\n") == -1 && strstr(s, "\r") == -1 && strstr(s, "\\") == -1)
    return s;
  r = "";
  n = strlen(s);
  for (i = 0; i < n; i++) {
    c = substr(s, i, i + 1);
    if (c == "\\")      r = r + "\\\\";
    else if (c == "\t") r = r + "\\t";
    else if (c == "\n") r = r + "\\n";
    else if (c == "\r") r = r + "\\r";
    else                r = r + c;
  }
  return r;
}

static hx(ea) {
  return form("0x%X", ea);
}

static err(out, msg) {
  fprintf(out, "#ERROR\t%s\n", esc(msg));
  return 1;
}

static info(out, key, value) {
  fprintf(out, "#INFO\t%s\t%s\n", key, esc(value));
}

static total(out, n) {
  fprintf(out, "#TOTAL\t%d\n", n);
}

// An address given either as a number or as a name (name wins when not empty).
static resolve(ea, name) {
  if (name != "")
    return LocByName(name);
  return ea;
}

static matches(s, filter) {
  if (filter == "")
    return 1;
  return strstr(s, filter) != -1;
}

// The first head at or after ea (unexplored bytes are skipped).
static first_head(ea, end) {
  if (isHead(GetFlags(ea)))
    return ea;
  return NextHead(ea, end);
}

static item_bytes(ea, maxn) {
  auto i, n, r;
  n = ItemSize(ea);
  if (n > maxn) n = maxn;
  r = "";
  for (i = 0; i < n; i++) {
    if (i > 0) r = r + " ";
    r = r + form("%02X", Byte(ea + i));
  }
  return r;
}
