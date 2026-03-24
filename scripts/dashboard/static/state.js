export const BASE = window.__BASE_PATH__ || "";

export const state = {
  tasks: [],
  jobs: [],
  routines: [],
  viewStack: [{ type: "tasks" }],
};
