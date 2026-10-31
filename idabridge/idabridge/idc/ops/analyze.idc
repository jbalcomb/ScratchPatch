// Runs after auto-analysis (main() calls Wait()); main() saves the database.
static op_analyze(out) {
  return op_info(out);
}
