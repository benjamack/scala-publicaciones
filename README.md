# Publicaciones de @scala.fit

Las publicaciones de @scala.fit que salen solas. Cada pieza sale en su día por la API oficial de Instagram, desde GitHub Actions. `agenda.json` dice qué sale y cuándo; `publicadas.json`, qué salió. Quien comenta SCALA en una publicación (de cualquier edad) o en una historia, o escribe SCALA por mensaje, recibe el link por privado; un mensaje más largo que la palabra lo contesta una persona.

## El ritmo

Cada corrida espera 5 minutos en el entorno `espera` (un temporizador de GitHub: no ocupa una máquina), trabaja y pide la siguiente. El `cron` queda de red por si la cadena se corta.

El entorno se crea una vez:

```
gh api -X PUT repos/<dueño>/<repo>/environments/espera -F wait_timer=5
```

Si una corrida no esperó, no pide la siguiente: sin ese freno, un entorno mal configurado sería un bucle sin pausa contra Instagram.

Para frenar la cadena: poner la variable del repo `CADENA` en `off` (`gh variable set CADENA --body off`), o cancelar la corrida que espera.
