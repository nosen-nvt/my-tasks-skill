import { state } from "./state.js";

let _renderFn = null;

export function setRenderer(fn) {
  _renderFn = fn;
}

export function currentView() {
  return state.viewStack[state.viewStack.length - 1];
}

export function pushView(view) {
  state.viewStack.push(view);
  _renderFn?.();
}

export function popView() {
  if (state.viewStack.length > 1) {
    state.viewStack.pop();
    _renderFn?.();
  }
}
