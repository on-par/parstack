// line comment
import { x } from "./x";

/* one-line block */
/**
 * multi-line doc
 */
export function f(a: number): number {
  const url = "http://example.invalid/*not-a-comment*/";
  return a + 1; // trailing comment counts as code
}
const y = 2; /* opens a block
still in block */ const z = 3;

/*
 *
 */
