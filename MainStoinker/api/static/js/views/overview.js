// Placeholder: the run overview is a later task.
import { comingSoon } from "../api.js";

export function render(container, params) {
  comingSoon(container, "Run " + params.runId, { runId: params.runId });
}
