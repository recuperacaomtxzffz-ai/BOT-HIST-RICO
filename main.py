
import os
import json
import base64
import sqlite3
import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands
from openai import OpenAI

# ================= CONFIGURAÇÕES =================

TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
NICK = os.getenv("PLAYER_NICK", "tokyezz")
DB_PATH = os.getenv("DATABASE_PATH", "partidas.db")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not TOKEN or not OPENAI_KEY:
    raise RuntimeError("Configure DISCORD_TOKEN e OPENAI_API_KEY no Railway.")

ai = OpenAI(api_key=OPENAI_KEY)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Usuários aguardando o envio de print
pending = {}
TIMEOUT = 180

MODOS = ["1x1", "2x2", "3x3", "4x4"]


# ================= BANCO DE DADOS =================

def conectar():
    return sqlite3.connect(DB_PATH)


def iniciar_banco():
    with conectar() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS partidas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario INTEGER NOT NULL,
                servidor INTEGER NOT NULL,
                resultado TEXT NOT NULL,
                modo TEXT NOT NULL,
                azul INTEGER NOT NULL,
                laranja INTEGER NOT NULL,
                data TEXT NOT NULL
            )
        """)


def registrar(usuario, servidor, resultado, modo, azul, laranja):
    with conectar() as con:
        con.execute("""
            INSERT INTO partidas
            (usuario, servidor, resultado, modo, azul, laranja, data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            usuario, servidor, resultado, modo, azul, laranja,
            datetime.now(timezone.utc).isoformat()
        ))


def estatisticas(usuario):
    with conectar() as con:
        vitorias = con.execute(
            "SELECT COUNT(*) FROM partidas WHERE usuario=? AND resultado='win'",
            (usuario,)
        ).fetchone()[0]

        derrotas = con.execute(
            "SELECT COUNT(*) FROM partidas WHERE usuario=? AND resultado='loss'",
            (usuario,)
        ).fetchone()[0]

    return vitorias, derrotas


# ================= ANÁLISE DO PRINT =================

def analisar_print(imagem, tipo, modo):
    encoded = base64.b64encode(imagem).decode("utf-8")

    mime = tipo if tipo in (
        "image/png", "image/jpeg", "image/webp"
    ) else "image/jpeg"

    prompt = f"""
Analise esta imagem de resultado final de Free Fire.

Responda somente JSON válido com:
{{
  "tela_resultado": true,
  "azul": 7,
  "laranja": 1,
  "nick_encontrado": true,
  "confianca": "alta",
  "observacao": "..."
}}

REGRAS:

1. Identifique o placar das equipes AZUL e LARANJA.
2. Não confunda o placar das equipes com K, A ou DMG.
3. A partida deve mostrar claramente a tela final.
4. Modo selecionado: {modo}.
5. Se o modo for 1x1, procure o nick "{NICK}" na lista de jogadores.
   O nick precisa estar visível e corresponder ao nome esperado.
6. Se o modo for 2x2, 3x3 ou 4x4, não exija nick.
7. Nunca invente placares ou nomes.
8. Se alguma informação estiver ilegível, use null.
9. Marque confiança baixa quando houver dúvida.
"""

    response = ai.chat.completions.create(
        model=MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{encoded}",
                        "detail": "high"
                    }
                }
            ]
        }]
    )

    return json.loads(response.choices[0].message.content)


def conferir(resultado, acao, modo):
    if resultado.get("tela_resultado") is not True:
        return False, "Não encontrei uma tela final de partida."

    if resultado.get("confianca") not in ("alta", "media"):
        return False, "O print está ilegível ou apresenta dúvidas."

    if modo == "1x1" and resultado.get("nick_encontrado") is not True:
        return False, f"Não encontrei o nick {NICK}."

    azul = resultado.get("azul")
    laranja = resultado.get("laranja")

    if type(azul) is not int or type(laranja) is not int:
        return False, "Não consegui identificar os dois placares."

    if acao == "win":
        valido = azul == 7 and 0 <= laranja <= 6
    else:
        valido = laranja == 7 and 0 <= azul <= 6

    if not valido:
        return False, (
            f"Placar identificado: Azul {azul} × Laranja {laranja}. "
            "Esse placar não corresponde ao resultado selecionado."
        )

    return True, f"Placar confirmado: Azul {azul} × Laranja {laranja}."


# ================= SELEÇÃO DO MODO =================

class SeletorModo(discord.ui.View):
    def __init__(self, acao):
        super().__init__(timeout=120)
        self.acao = acao

        select = discord.ui.Select(
            placeholder="Escolha o modo da partida",
            options=[
                discord.SelectOption(label=modo, value=modo)
                for modo in MODOS
            ],
            custom_id="seletor_modo_ff"
        )
        select.callback = self.selecionar
        self.add_item(select)

    async def selecionar(self, interaction: discord.Interaction):
        modo = interaction.data["values"][0]
        uid = interaction.user.id

        if uid in pending:
            await interaction.response.send_message(
                "Você já tem uma partida aguardando print.",
                ephemeral=True
            )
            return

        pending[uid] = {
            "acao": self.acao,
            "modo": modo,
            "canal": interaction.channel_id,
            "servidor": interaction.guild_id
        }

        nick_info = (
            f"Vou conferir o nick **{NICK}** e o placar."
            if modo == "1x1"
            else "Vou conferir somente o placar."
        )

        await interaction.response.send_message(
            f"✅ Modo selecionado: **{modo}**\n"
            f"📸 Agora envie o print da partida neste canal.\n"
            f"{nick_info}\n"
            "Você tem 3 minutos para enviar.",
            ephemeral=True
        )

        async def expirar():
            await asyncio.sleep(TIMEOUT)
            item = pending.get(uid)
            if item and item["modo"] == modo and item["acao"] == self.acao:
                pending.pop(uid, None)
                try:
                    await interaction.followup.send(
                        "⌛ Tempo esgotado. Clique novamente no painel.",
                        ephemeral=True
                    )
                except discord.HTTPException:
                    pass

        asyncio.create_task(expirar())


# ================= PAINEL PRINCIPAL =================

class Painel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="VITÓRIA",
        emoji="🏆",
        style=discord.ButtonStyle.success,
        custom_id="ff_vitoria"
    )
    async def vitoria(self, interaction, button):
        if interaction.user.id in pending:
            await interaction.response.send_message(
                "Você já está aguardando um print.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "🏆 **Você selecionou VITÓRIA!**\nEscolha o modo:",
            view=SeletorModo("win"),
            ephemeral=True
        )

    @discord.ui.button(
        label="DERROTA",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="ff_derrota"
    )
    async def derrota(self, interaction, button):
        if interaction.user.id in pending:
            await interaction.response.send_message(
                "Você já está aguardando um print.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "❌ **Você selecionou DERROTA!**\nEscolha o modo:",
            view=SeletorModo("loss"),
            ephemeral=True
        )

    @discord.ui.button(
        label="HISTÓRICO",
        emoji="📊",
        style=discord.ButtonStyle.primary,
        custom_id="ff_historico"
    )
    async def historico(self, interaction, button):
        vitorias, derrotas = estatisticas(interaction.user.id)
        total = vitorias + derrotas
        taxa = vitorias / total * 100 if total else 0

        embed = discord.Embed(
            title="📊 HISTÓRICO DE PARTIDAS",
            color=discord.Color.blurple()
        )
        embed.add_field(name="🏆 Vitórias", value=vitorias)
        embed.add_field(name="❌ Derrotas", value=derrotas)
        embed.add_field(name="🎮 Total", value=total)
        embed.add_field(name="📈 Aproveitamento", value=f"{taxa:.1f}%")

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


# ================= RECEBER PRINT =================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    uid = message.author.id
    item = pending.get(uid)

    if item and message.channel.id == item["canal"]:
        arquivo = next(
            (
                a for a in message.attachments
                if a.content_type and a.content_type.startswith("image/")
            ),
            None
        )

        if arquivo is None:
            await message.reply("📸 Envie o print como imagem PNG, JPG ou WEBP.")
            return

        pending.pop(uid, None)

        resposta = await message.reply("🔎 Analisando seu print...")

        try:
            if arquivo.size > 8 * 1024 * 1024:
                await resposta.edit(
                    content="A imagem é muito grande. Envie uma imagem de até 8 MB."
                )
                return

            imagem = await arquivo.read()

            resultado = await asyncio.to_thread(
                analisar_print,
                imagem,
                arquivo.content_type,
                item["modo"]
            )

            valido, mensagem = conferir(
                resultado,
                item["acao"],
                item["modo"]
            )

            if not valido:
                await resposta.edit(
                    content=f"❌ **PARTIDA NÃO REGISTRADA**\n{mensagem}"
                )
                return

            registrar(
                uid,
                item["servidor"],
                item["acao"],
                item["modo"],
                resultado["azul"],
                resultado["laranja"]
            )

            vitorias, derrotas = estatisticas(uid)

            titulo = (
                "🏆 VITÓRIA REGISTRADA!"
                if item["acao"] == "win"
                else "❌ DERROTA REGISTRADA!"
            )

            cor = (
                discord.Color.green()
                if item["acao"] == "win"
                else discord.Color.red()
            )

            embed = discord.Embed(
                title=titulo,
                description=mensagem,
                color=cor
            )

            embed.add_field(name="Modo", value=item["modo"])
            embed.add_field(name="Nick", value=(
                NICK if item["modo"] == "1x1" else "Não exigido"
            ))
            embed.add_field(
                name="Seu histórico",
                value=f"🏆 {vitorias} | ❌ {derrotas}"
            )
            embed.set_image(url=arquivo.url)

            await resposta.edit(content=None, embed=embed)

        except Exception as erro:
            print("Erro na análise:", repr(erro))
            await resposta.edit(
                content="⚠️ Erro ao analisar. A partida não foi contabilizada."
            )

        return

    await bot.process_commands(message)


# ================= COMANDO PARA CRIAR PAINEL =================

@bot.command(name="painel")
@commands.has_permissions(administrator=True)
async def painel(ctx):
    embed = discord.Embed(
        title="🏆 FF MATCH SYSTEM",
        description=(
            "Sistema automático de registro de partidas.\n\n"
            "🏆 **VITÓRIA** — Registrar uma vitória.\n"
            "❌ **DERROTA** — Registrar uma derrota.\n"
            "📊 **HISTÓRICO** — Consultar suas estatísticas.\n\n"
            f"👤 Nick do modo 1x1: **{NICK}**\n"
            "🔵 Sua equipe: **AZUL**\n"
            "🎯 Meta: **7 pontos**"
        ),
        color=discord.Color.blue()
    )

    embed.set_footer(text="FF MATCH SYSTEM • Verificação de partidas")

    await ctx.send(embed=embed, view=Painel())


@bot.event
async def on_ready():
    iniciar_banco()
    bot.add_view(Painel())
    print(f"Bot conectado: {bot.user}")


bot.run(TOKEN)
