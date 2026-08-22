/**
 * The single hydrated root.
 *
 * This exists because of an Astro rule that is easy to get wrong: a framework
 * component passed as a *child* of a hydrated component arrives as a slot, which
 * is static HTML. Writing
 *
 *     <Boundary client:load><Console /></Boundary>
 *
 * hydrates the boundary and freezes the console inside it, so the page renders
 * once on the server and then never runs any JavaScript again. It looks like a
 * backend outage — empty selector, nothing connected, no error, because the code
 * that would report the error is not running either.
 *
 * Composing the two in React and hydrating this one component keeps them in a
 * single tree.
 */

import { Boundary } from "./Boundary";
import { Console } from "./Console";

export function App() {
  return (
    <Boundary>
      <Console />
    </Boundary>
  );
}
