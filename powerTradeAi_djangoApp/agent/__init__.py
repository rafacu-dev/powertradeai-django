"""Agente de PowerTradeAI.

Un agente que razona sobre el mercado con un LLM y un conjunto de *skills*
(herramientas) que puede consultar: datos de mercado, opciones, el scanner de
Bollinger, su propio analisis previo, y crear alertas marcadas como suyas.

Todo su proceso de pensamiento queda registrado en ``AgentRun`` y su vision de
cada activo en ``AgentAnalysis``, para dar continuidad entre corridas.
"""
