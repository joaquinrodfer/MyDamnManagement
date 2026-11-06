// D8 (a): los bloques vacíos de los extremos no existen.
//
// El `.md` ya se guardaba sin ellos (el backend canónico quita el salto
// separador y los finales de línea), así que el editor no puede prometer un
// bloque que desaparece al recargar. `canonicalBody` es la regla que comparten
// el guardado y el recorte del propio documento, y `cursorInOuterBlanks` la
// que decide cuándo se puede recortar (nunca bajo el cursor).
//
// Los tres casos del informe, tal cual: `Intro Intro x`, `y Intro Intro` y
// `A Mayús+Intro Mayús+Intro`.
//
// Lo mismo vale para el bloque vacío que se crea en MEDIO (el «+» del asa, ⏎
// al final de un bloque con contenido detrás): se colapsa al abandonarlo, pero
// solo si lo ha creado el editor -- `emptyGapRange` / `collapseEmptyGaps` --,
// así que un `alfa\n\n\n\nbeta` escrito a mano se sigue guardando tal cual.

import test from "node:test";
import assert from "node:assert/strict";

import {
  canonicalBody,
  collapseEmptyGaps,
  cursorInOuterBlanks,
  emptyGapRange,
  newlineRunAt,
  outerBlankRange,
  shiftEmptyGap,
  withoutRange,
} from "../body.js";

test("D8: dos Intro al principio se van con el texto", () => {
  assert.equal(canonicalBody("\n\nx"), "x");
});

test("D8: un Intro al principio se va (el backend guarda un solo separador)", () => {
  assert.equal(canonicalBody("\nx"), "x");
});

test("D8: los saltos del final se van", () => {
  assert.equal(canonicalBody("y\n\n"), "y");
});

test("D8: los dos bloques vacíos de dos Mayús+Intro se van", () => {
  assert.equal(canonicalBody("A\n\n\n\n"), "A");
});

test("las líneas en blanco de en medio NO se tocan", () => {
  assert.equal(canonicalBody("a\n\n\nb"), "a\n\n\nb");
  assert.equal(canonicalBody("a\n\nb\n"), "a\n\nb");
});

test("un documento vacío sigue vacío", () => {
  assert.equal(canonicalBody(""), "");
  assert.equal(canonicalBody("\n\n"), "");
  assert.equal(canonicalBody(null), "");
});

test("outerBlankRange: dónde empieza y acaba el contenido", () => {
  assert.deepEqual(outerBlankRange("\n\nabc\n"), { lead: 2, end: 5 });
  assert.deepEqual(outerBlankRange("abc"), { lead: 0, end: 3 });
  assert.deepEqual(outerBlankRange("\n\n"), { lead: 2, end: 0 });
});

test("el cursor dentro del hueco de los extremos impide recortar", () => {
  // "\n\nx": el hueco de delante son las posiciones 0 y 1; 2 es la "x".
  assert.equal(cursorInOuterBlanks("\n\nx", 0), true);
  assert.equal(cursorInOuterBlanks("\n\nx", 1), true);
  assert.equal(cursorInOuterBlanks("\n\nx", 2), false);
  // "x\n\n": el hueco de detrás empieza en 1 (final de la "x").
  assert.equal(cursorInOuterBlanks("x\n\n", 1), false);
  assert.equal(cursorInOuterBlanks("x\n\n", 2), true);
  assert.equal(cursorInOuterBlanks("x\n\n", 3), true);
});

// ------------------------------------------ bloque vacío de en medio (t_e9087348)

test("el hueco del + en medio: doc con 4 saltos, se colapsa a los 2 de separador", () => {
  // `alfa\n\nbeta` + «+» en el primer bloque -> se inserta «\n\n» en 4.
  const doc = "alfa\n\n\n\nbeta";
  assert.deepEqual(emptyGapRange(doc, { from: 4, to: 6 }), { from: 4, to: 6 });
  assert.deepEqual(collapseEmptyGaps(doc, [{ from: 4, to: 6 }]), {
    text: "alfa\n\nbeta",
    ranges: [{ from: 4, to: 6 }],
  });
});

test("sin marcas no se toca nada: las líneas en blanco de en medio son del usuario", () => {
  assert.deepEqual(collapseEmptyGaps("alfa\n\n\n\nbeta", []), {
    text: "alfa\n\n\n\nbeta",
    ranges: [],
  });
});

test("si se escribe en el bloque nuevo, el hueco es contenido", () => {
  // «+» y luego teclear: el par queda delante del texto, no de un salto.
  const doc = "alfa\n\ngamma\n\nbeta";
  assert.equal(emptyGapRange(doc, { from: 4, to: 6 }), null);
  assert.deepEqual(collapseEmptyGaps(doc, [{ from: 4, to: 6 }]), { text: doc, ranges: [] });
});

test("alfa\\n\\n\\n\\nbeta escrito a mano + ⏎: se va el par del bloque nuevo, no los suyos", () => {
  // Cuatro saltos de la nota + el par que inserta ⏎ al final de «alfa».
  const doc = "alfa\n\n\n\n\n\nbeta";
  assert.deepEqual(collapseEmptyGaps(doc, [{ from: 4, to: 6 }]), {
    text: "alfa\n\n\n\nbeta",
    ranges: [{ from: 4, to: 6 }],
  });
});

test("el hueco también vale pegado al separador de DELANTE (⏎ sobre un bloque marcado)", () => {
  // `alfa\n\nbeta\n\ngamma` con beta marcado: el par se inserta detrás del
  // separador que ya había, así que el hueco queda entre dos saltos.
  const doc = "alfa\n\n\n\ngamma";
  assert.deepEqual(emptyGapRange(doc, { from: 6, to: 8 }), { from: 6, to: 8 });
  assert.deepEqual(collapseEmptyGaps(doc, [{ from: 6, to: 8 }]), {
    text: "alfa\n\ngamma",
    ranges: [{ from: 6, to: 8 }],
  });
});

test("un hueco ya inexistente o desplazado no rompe nada", () => {
  assert.equal(emptyGapRange("alfabeta", { from: 4, to: 6 }), null);
  assert.equal(emptyGapRange("alfa\n\nbeta", { from: 4, to: 4 }), null);
  assert.equal(emptyGapRange("alfa\n\nbeta", null), null);
  assert.deepEqual(collapseEmptyGaps("alfa\n\nbeta", [{ from: 4, to: 6 }]), {
    text: "alfa\n\nbeta",
    ranges: [],
  });
});

test("dos marcas en el mismo hueco se quitan una sola vez", () => {
  assert.deepEqual(collapseEmptyGaps("alfa\n\n\n\nbeta", [{ from: 4, to: 6 }, { from: 4, to: 6 }]), {
    text: "alfa\n\nbeta",
    ranges: [{ from: 4, to: 6 }],
  });
});

test("dos huecos distintos en el mismo documento se quitan los dos", () => {
  // alfa |+en blanco| beta |+en blanco| gamma
  const doc = "alfa\n\n\n\nbeta\n\n\n\ngamma";
  assert.deepEqual(collapseEmptyGaps(doc, [{ from: 4, to: 6 }, { from: 12, to: 14 }]), {
    text: "alfa\n\nbeta\n\ngamma",
    ranges: [{ from: 4, to: 6 }, { from: 12, to: 14 }],
  });
});

test("un par de saltos al final del documento no es un hueco interior", () => {
  assert.equal(emptyGapRange("alfa\n\n", { from: 4, to: 6 }), null);
});

test("newlineRunAt: el tramo de saltos que contiene la posición", () => {
  assert.deepEqual(newlineRunAt("alfa\n\n\n\nbeta", 6), { from: 4, to: 8 });
  assert.deepEqual(newlineRunAt("alfa\n\n\n\nbeta", 4), { from: 4, to: 8 });
  assert.equal(newlineRunAt("alfa\n\nbeta", 2), null); // dentro de «alfa»
  assert.equal(newlineRunAt("beta", 0), null);
  assert.deepEqual(newlineRunAt("\n\n", 1), { from: 0, to: 2 });
});

// ------------------------- desplazamiento de la marca con las ediciones
// El hueco se marca con las posiciones de sus dos saltos y hay que moverlo con
// cada transacción (`shiftEmptyGap`, que es lo que usa el StateField). Aquí
// está el fallo que se colaba: con el sesgo de `mapPos` hacia fuera en las dos
// puntas, un texto insertado justo en `from` alargaba el rango a 4, la marca
// se descartaba y el aire se quedaba para siempre en el `.md`.

test("dos «+» seguidos en el mismo bloque: la marca vieja se desplaza, no se pierde", () => {
  // `alfa\n\nbeta` + «+» en el bloque 0 -> `alfa\n\n\n\nbeta` con la marca
  // {4,6}. El segundo «+» inserta otro par en 4, DELANTE del primer hueco.
  assert.deepEqual(shiftEmptyGap({ from: 4, to: 6 }, [{ from: 4, to: 4, insert: "\n\n" }]), {
    from: 6,
    to: 8,
  });
  // Y con las dos marcas el documento vuelve al canónico al abandonarlas.
  assert.deepEqual(
    collapseEmptyGaps("alfa\n\n\n\n\n\nbeta", [
      { from: 6, to: 8 }, // la del primer «+», desplazada
      { from: 4, to: 6 }, // la que añade el segundo
    ]),
    {
      text: "alfa\n\nbeta",
      ranges: [
        { from: 4, to: 6 },
        { from: 6, to: 8 },
      ],
    },
  );
});

test("tres «+» seguidos: una marca por par y se quitan todas", () => {
  const doc = "alfa\n\n\n\n\n\n\n\nbeta";
  assert.deepEqual(shiftEmptyGap({ from: 6, to: 8 }, [{ from: 4, to: 4, insert: "\n\n" }]), {
    from: 8,
    to: 10,
  });
  assert.deepEqual(
    collapseEmptyGaps(doc, [
      { from: 4, to: 6 },
      { from: 6, to: 8 },
      { from: 8, to: 10 },
    ]),
    {
      text: "alfa\n\nbeta",
      ranges: [
        { from: 4, to: 6 },
        { from: 6, to: 8 },
        { from: 8, to: 10 },
      ],
    },
  );
});

test("lo que se escribe justo detrás del par no lo alarga: ese hueco ya es contenido", () => {
  assert.deepEqual(shiftEmptyGap({ from: 4, to: 6 }, [{ from: 6, to: 6, insert: "x" }]), {
    from: 4,
    to: 6,
  });
  assert.equal(emptyGapRange("alfa\n\nx\n\nbeta", { from: 4, to: 6 }), null);
});

test("escribir DENTRO del par descarta la marca: ya no mide dos saltos", () => {
  assert.equal(shiftEmptyGap({ from: 4, to: 6 }, [{ from: 5, to: 5, insert: "x" }]), null);
});

test("borrar el par, o el texto que lo envuelve, descarta la marca", () => {
  assert.equal(shiftEmptyGap({ from: 4, to: 6 }, [{ from: 4, to: 6, insert: "" }]), null);
  assert.equal(shiftEmptyGap({ from: 4, to: 6 }, [{ from: 2, to: 8, insert: "" }]), null);
});

test("un cambio antes del hueco lo desplaza entero (insertar y reemplazar)", () => {
  assert.deepEqual(shiftEmptyGap({ from: 4, to: 6 }, [{ from: 0, to: 0, insert: "xy" }]), {
    from: 6,
    to: 8,
  });
  assert.deepEqual(shiftEmptyGap({ from: 4, to: 6 }, [{ from: 0, to: 3, insert: "z" }]), {
    from: 2,
    to: 4,
  });
});

test("varios cambios en la misma transacción (el recorte interno) descartan sus propios pares", () => {
  // `dropInnerEmptyGap` despacha los dos huecos en una sola transacción.
  const changes = [
    { from: 4, to: 6, insert: "" },
    { from: 12, to: 14, insert: "" },
  ];
  assert.equal(shiftEmptyGap({ from: 4, to: 6 }, changes), null);
  assert.equal(shiftEmptyGap({ from: 12, to: 14 }, changes), null);
});

test("sin cambios la marca se queda donde estaba; una marca inválida no rompe", () => {
  assert.deepEqual(shiftEmptyGap({ from: 4, to: 6 }, []), { from: 4, to: 6 });
  assert.equal(shiftEmptyGap(null, []), null);
  // Un cambio con from > to no es un cambio: se ignora.
  assert.deepEqual(shiftEmptyGap({ from: 4, to: 6 }, [{ from: 9, to: 3, insert: "x" }]), {
    from: 4,
    to: 6,
  });
});

// ---- el filtro del selector de bases no es texto de la nota ----------------
//
// Se escribe dentro del documento para poder filtrar, pero lo que se guarda es
// el documento sin él: el autoguardado y el guardado de emergencia al cerrar o
// recargar la pestaña no pasan por ningún cierre del selector (`getValue` en
// entry.js -> `withoutRange` aquí).

test("withoutRange: quita el tramo y deja el texto de alrededor", () => {
  const out = withoutRange("abcdef", [], { from: 2, to: 4 });
  assert.equal(out.text, "abef");
  assert.deepEqual(out.gaps, []);
});

test("withoutRange: una marca de hueco que cae DENTRO del tramo desaparece", () => {
  // "ab\n\ncd" -> se quita 1..4 ("b\n\n"): el par deja de medir dos saltos.
  const out = withoutRange("ab\n\ncd", [{ from: 2, to: 4 }], { from: 1, to: 4 });
  assert.equal(out.text, "acd");
  assert.deepEqual(out.gaps, []);
});

test("withoutRange: las marcas de DELANTE del tramo se quedan donde estaban", () => {
  const out = withoutRange("ab\n\ncdefgh", [{ from: 2, to: 4 }], { from: 6, to: 8 });
  assert.equal(out.text, "ab\n\ncdgh");
  assert.deepEqual(out.gaps, [{ from: 2, to: 4 }]);
});

test("withoutRange: las marcas de DETRÁS se corren lo que se ha quitado", () => {
  const out = withoutRange("abcdefgh\n\n", [{ from: 4, to: 6 }], { from: 0, to: 4 });
  assert.equal(out.text, "efgh\n\n");
  assert.deepEqual(out.gaps, [{ from: 0, to: 2 }]);
});

test("withoutRange: sin tramo (filtro vacío) no cambia nada", () => {
  const gaps = [{ from: 2, to: 4 }];
  assert.deepEqual(withoutRange("ab\n\ncd", gaps, { from: 3, to: 3 }), {
    text: "ab\n\ncd",
    gaps,
  });
  assert.deepEqual(withoutRange("ab\n\ncd", gaps, null), { text: "ab\n\ncd", gaps });
});

test("withoutRange: un tramo al revés o fuera del documento no rompe nada", () => {
  assert.equal(withoutRange("abcdef", [], { from: 4, to: 1 }).text, "abcdef");
  const out = withoutRange("abcdef", [], { from: 50, to: 90 });
  assert.equal(out.text, "abcdef");
  // Se recorta al documento, no se sale de él.
  assert.equal(withoutRange("abcdef", [], { from: 2, to: 99 }).text, "ab");
  assert.equal(withoutRange("abcdef", [], { from: -5, to: 2 }).text, "cdef");
});

test("withoutRange: el caso de la tarjeta -- el filtro no llega al .md", () => {
  // "# T\n\ncontactos.\n\nfiltro", con el selector abierto al final.
  const text = "# T\n\ncontactos.\n\nfiltro";
  const from = text.indexOf("filtro");
  const out = withoutRange(text, [{ from: 15, to: 17 }], { from, to: text.length });
  assert.equal(canonicalBody(out.text), "# T\n\ncontactos.");
  // La marca del separador sigue siendo válida para el recorte normal: cae ya
  // al final del documento, así que no colapsa nada.
  assert.equal(out.gaps.length, 1);
  assert.equal(canonicalBody(collapseEmptyGaps(out.text, out.gaps).text), "# T\n\ncontactos.");
});
