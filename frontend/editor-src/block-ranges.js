// De árbol de Markdown a BLOQUES: la lista de trozos de texto que la app trata
// como bloques (asa, `+`, Alt+↑/↓, arrastre, «Convertir a»...).
//
// Vive aparte de entry.js para que el cálculo sea el mismo que usan los tests
// de `node --test` sobre el árbol de verdad (`markdownLanguage.parser`), y no
// una copia con párrafos inventados: mover un bloque depende de dónde acaban
// los rangos, y una copia que se desviara escondería justo los fallos que se
// buscan.
//
// Las vallas de código son UN bloque (FencedCode va de la apertura al cierre),
// y una lista plana son N bloques (uno por ListItem hijo directo): una lista
// anidada queda dentro del ítem, así que mover el ítem se lleva sus hijos.
//
// Un bloque va de principio a fin de SUS líneas: la sangría (los 4 espacios de
// un bloque de código indentado, los de una lista o una cita metidas hacia
// dentro) es texto del bloque, no del hueco que lo separa del de arriba. Si esa
// sangría se quedara en el hueco, mover el bloque la perdería (ver
// `lineStartOf`).

/**
 * Principio de la línea de `pos`, mientras lo que haya entre medias sean solo
 * espacios o tabuladores.
 *
 * Un bloque empieza donde empieza SU LÍNEA, no en su primer carácter con
 * contenido: la sangría (los 4 espacios de un bloque de código indentado, los
 * de una lista metida 2 espacios, los de una cita) es texto del bloque como
 * cualquier otro. Dejándola fuera se queda en el HUECO entre bloques, y el hueco
 * no siempre se puede copiar tal cual: en cuanto hay que rehacerlo (mover un
 * bloque, `fitGap`) la sangría desaparece -- `"    codigo"` se queda en
 * `"codigo"` y el bloque deja de ser código. Tarjeta t_9d3b6d3b.
 *
 * `doc` es el documento: un `string` o un `Text` de CodeMirror (basta con
 * `sliceString`, así que no hace falta copiarlo entero en cada edición). Se lee
 * hacia atrás en tramos, no carácter a carácter.
 */
function lineStartOf(doc, pos) {
  let p = pos;
  while (p > 0) {
    const start = Math.max(0, p - 256);
    const chunk = typeof doc === "string" ? doc.slice(start, p) : doc.sliceString(start, p);
    let k = chunk.length;
    while (k > 0 && (chunk[k - 1] === " " || chunk[k - 1] === "\t")) k--;
    p = start + k;
    if (k > 0) break; // un carácter que no es sangría (o el salto de línea)
  }
  return p;
}

/**
 * `doc` es el texto del documento, y hace falta: sin él no se sabe dónde acaba
 * la sangría de un bloque (ver `lineStartOf`). Con un árbol suelto y sin texto
 * el rango se queda en el primer carácter con contenido -- lo que había antes,
 * que solo sirve para documentos sin bloques indentados.
 */
export function blocksFromTree(root, doc) {
  const blocks = [];
  const from = (node) => (doc == null ? node.from : lineStartOf(doc, node.from));
  for (let node = root.firstChild; node; node = node.nextSibling) {
    switch (node.name) {
      case "BlankLine":
      case "CommentBlock":
        break;
      case "BulletList":
      case "OrderedList": {
        const itemType = node.name === "BulletList" ? "bullet_list_item" : "ordered_list_item";
        for (let item = node.firstChild; item; item = item.nextSibling) {
          if (item.name === "ListItem") blocks.push({ from: from(item), to: item.to, type: itemType });
        }
        break;
      }
      case "ATXHeading1":
        blocks.push({ from: from(node), to: node.to, type: "h1" });
        break;
      case "ATXHeading2":
        blocks.push({ from: from(node), to: node.to, type: "h2" });
        break;
      case "ATXHeading3":
        blocks.push({ from: from(node), to: node.to, type: "h3" });
        break;
      case "ATXHeading4":
        blocks.push({ from: from(node), to: node.to, type: "h4" });
        break;
      case "ATXHeading5":
      case "ATXHeading6":
        blocks.push({ from: from(node), to: node.to, type: "h5" });
        break;
      case "HorizontalRule":
        blocks.push({ from: from(node), to: node.to, type: "hr" });
        break;
      case "FencedCode":
      case "CodeBlock":
        // La valla empieza en su primera comilla; el código INDENTADO empieza en
        // la línea, con sus 4 espacios (o su tabulador) dentro del bloque.
        blocks.push({ from: from(node), to: node.to, type: "code" });
        break;
      case "Paragraph":
        blocks.push({ from: from(node), to: node.to, type: "paragraph" });
        break;
      case "Blockquote":
        // Un solo bloque para toda la cita, con independencia de cuántos
        // niveles anidados (">", ">>"...) tenga dentro -- solo se recorren
        // los hijos DIRECTOS del documento aquí, así que una cita anidada
        // (Blockquote dentro de otro Blockquote) nunca se cuenta aparte.
        // La profundidad de cada línea se calcula al pintarla (ver
        // quoteDecorations), no aquí.
        blocks.push({ from: from(node), to: node.to, type: "quote" });
        break;
      default:
        // HTMLBlock, Table, LinkReference... siguen siendo
        // seleccionables/borrables, solo no convertibles.
        blocks.push({ from: from(node), to: node.to, type: "other" });
    }
  }
  blocks.sort((a, b) => a.from - b.from);
  return blocks;
}
