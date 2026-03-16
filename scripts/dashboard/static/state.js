export const BASE = window.__BASE_PATH__ || "";

export const state = {
  tasks: [],
  lifecycles: [],
  jobs: [],
  viewStack: [{ type: "tasks" }],
};
