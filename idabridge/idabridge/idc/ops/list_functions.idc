static op_list_functions(out, filter, offset, limit) {
  auto ea, end, name, n;
  n = 0;
  ea = MinEA();
  if (GetFunctionAttr(ea, FUNCATTR_START) != ea) ea = NextFunction(ea);
  for (; ea != BADADDR; ea = NextFunction(ea)) {
    name = GetFunctionName(ea);
    if (!matches(name, filter)) continue;
    if (n >= offset && n < offset + limit) {
      end = GetFunctionAttr(ea, FUNCATTR_END);
      fprintf(out, "%s\t%s\t%d\t%s\t%s\n", hx(ea), hx(end), end - ea, esc(name), hx(GetFunctionFlags(ea)));
    }
    n = n + 1;
  }
  total(out, n);
  return 0;
}
