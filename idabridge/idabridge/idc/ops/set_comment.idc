static op_set_comment(out, ea, name, text, repeatable) {
  auto ok;
  ea = resolve(ea, name);
  if (ea == BADADDR) return err(out, "address or name not found");
  if (repeatable) ok = MakeRptCmt(ea, text);
  else ok = MakeComm(ea, text);
  if (!ok) return err(out, "IDA refused the comment");
  info(out, "ea", hx(ea));
  return 0;
}
