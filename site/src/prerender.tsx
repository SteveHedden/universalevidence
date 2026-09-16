import { renderToString } from "react-dom/server";
import { App } from "./App";

export function renderHome() {
  return renderToString(<App />);
}
