# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.tools.float_utils import float_compare, float_is_zero, float_round
from odoo.exceptions import UserError


class StockIpv(models.Model):
    _name = 'stock.ipv'
    _description = 'Stock IPV'
    _order = 'create_date desc'

    name = fields.Char(required=True, copy=False, default='New')

    requested_by = fields.Many2one(
        'res.users', 'Requested by', required=True,
        default=lambda s: s.env.uid, readonly=True
    )

    location_id = fields.Many2one('stock.location', 'Origen',
                                  domain=[('usage', 'in', ['internal'])],
                                  default=lambda self: self.env.ref('stock.stock_location_stock').id,
                                  required=True,
                                  readonly=True,
                                  states={'draft': [('readonly', False)]}
                                  )

    # FIXME "Si cambia el Destino deberia reflejarse en todas las move not done"
    location_dest_id = fields.Many2one('stock.location', 'Destino',
                                       domain=[('usage', 'in', ['internal'])],
                                       default=lambda self: self.env.ref('stock_ipv.ipv_location_destiny').id,
                                       required=True,
                                       readonly=True,
                                       states={'draft': [('readonly', False)]},
                                       help='Stock Destiny Location',
                                       )

    picking_id = fields.Many2one('stock.picking')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('check', 'Check'),
        ('assign', 'Ready'),
        ('open', 'Open'),
        ('close', 'Close'),
        ('cancel', 'Cancelled'),
    ], string='Status', compute='_compute_state',
        copy=False, index=True, readonly=True, store=True, track_visibility='onchange',
        help=" * Draft: No ha sido confirmado.\n"
             " * Ready: Chequeado disponibilidad y reservada las cantidades, listo para ser abierto.\n"
             " * Open: Las cantidades han sido movidas, no hay retorno, se puede agregar mas cantidades y productos.\n"
             " * Close: Esta bloqueado y no se puede editar mas.\n")

    ipv_lines = fields.One2many('stock.ipv.line', 'ipv_id')

    raw_lines = fields.One2many('stock.ipv.line', compute='compute_raw_lines')

    move_lines = fields.One2many('stock.move', compute='_compute_move_lines', string='Move Lines')

    show_check_availability = fields.Boolean(
        compute='_compute_show_check_availability',
        help='Technical field used to compute whether the check availability button should be shown.')

    show_open = fields.Boolean(
        compute='_compute_show_open',
        help='Technical field used to compute whether the Open button should be shown.')
    is_locked = fields.Boolean(default=True, help='When the picking is not done this allows changing the '
                                                  'initial demand. When the picking is done this allows '
                                                  'changing the done quantities.')

    date_open = fields.Datetime('Open date', copy=False, readonly=True,
                                help="Date at which the turn has been processed or cancelled.")

    date_close = fields.Datetime('Close date', copy=False, readonly=True,
                                 help="Date at which the turn was closed.")

    @api.depends('ipv_lines')
    def compute_raw_lines(self):
        self.ensure_one()
        if not self.ipv_lines:
            return {}
        self.raw_lines = self.ipv_lines.mapped('raw_ids')

    @api.depends('ipv_lines.move_ids', 'raw_lines.move_ids')
    def _compute_move_lines(self):
        self.move_lines = (self.ipv_lines + self.raw_lines).mapped('move_ids')

    @api.onchange('location_dest_id')
    def _compute_child_lines(self):
        dest = self.location_dest_id
        IPVl = self.env['stock.ipv.line']
        # Quant = self.env['stock.quant']
        sublocations = dest.child_ids
        self.ipv_lines = []
        ipvls = []
        for sublocation in sublocations:
            product = self.env['product.product'].search([('name', '=', sublocation.name)], limit=1)

            if sublocation.quant_ids:
                data = {
                    'product_id': product.id,
                    'raw_ids': []
                }

                if sublocation.usage == 'production':
                    for quant in sublocation.quant_ids:
                        raw = {
                            'product_id': quant.product_id.id
                        }
                        data['raw_ids'].append((0, 0, raw))
                ipvls.append((0, 0, data))
        self.update({'ipv_lines': ipvls})

    # @api.depends('ipv_lines')
    # def onchange_ipv_lines(self):
    #     return {'domain': {
    #         'product_id': [('name', 'not in', self.ipv_lines.mapped('name'))]
    #     }}

    @api.depends('picking_id.state')
    def _compute_state(self):
        ''' State of a picking depends on the state of its related stock.move
        - Draft: only used for "planned pickings"
        - Waiting: if the picking is not ready to be sent so if
          - (a) no quantity could be reserved at all or if
          - (b) some quantities could be reserved and the shipping policy is "deliver all at once"
        - Waiting another move: if the picking is waiting for another move
        - Ready: if the picking is ready to be sent so if:
          - (a) all quantities are reserved or if
          - (b) some quantities could be reserved and the shipping policy is "as soon as possible"
        - Done: if the picking is done.
        - Cancelled: if the picking is cancelled '''

        if not self.picking_id:
            self.state = 'draft'
        elif self.picking_id.state == 'draft':
            self.state = 'draft'
        elif self.picking_id.state in ['assigned']:
            self.state = 'assign'
        elif self.picking_id.state in ['done']:
            self.state = 'open'
        else:
            self.state = 'check'

    @api.depends('ipv_lines.request_qty', 'state')
    def _compute_show_check_availability(self):
        self.ensure_one()
        has_moves_to_reserve = any(float_compare(ipvl.request_qty, 0, precision_rounding=ipvl.product_uom.rounding)
                                   and ipvl.state not in ['assigned', 'done']
                                   for ipvl in self.ipv_lines
                                   )

        self.show_check_availability = self.is_locked and self.state not in (
            'close') and has_moves_to_reserve

    @api.multi
    @api.depends('state', 'is_locked')
    def _compute_show_open(self):
        self.ensure_one()
        self.show_open = self.is_locked and (self.state in 'assign')

    @api.model
    def create(self, vals):
        if vals.get('name', 'New') == 'New':
            vals['name'] = self.env['ir.sequence'].next_by_code('stock.ipv.seq')
        res = super().create(vals)
        return res

    def unlink(self):
        for ipv in self:
            if ipv.state not in ['draft', 'cancel']:
                raise UserError('No puede borrar un IPV que no este cancelado.')
            return super(StockIpv, self).unlink()

    @api.one
    def action_confirm(self):
        # call `_action_confirm` on every draft ipv line
        ipvl_confirmed = self.ipv_lines.filtered(lambda ipvl: ipvl.state == 'draft').action_confirm()

        if ipvl_confirmed:
            self.picking_id = ipvl_confirmed.mapped('move_ids.picking_id')
        return True

    @api.one
    def action_assign(self):
        """ Check availability of picking moves.
        This has the effect of changing the state and reserve quants on available moves, and may
        also impact the state of the picking as it is computed based on move's states.
        @return: True
        """
        self.filtered(lambda ipv: ipv.state == 'draft').action_confirm()

        # Cuando se confirma las movidas se crean los picking asociados
        self.picking_id.action_assign()
        return True

    @api.one
    def button_open(self):
        for move in self.move_lines:
            if move.is_quantity_done_editable:
                move.quantity_done = move.product_uom_qty
            else:
                for ml in move.move_line_ids:
                    ml.qty_done = ml.product_uom_qty

        self.picking_id.button_validate()
        self.write({
            'state': 'open',
            'date_open': fields.Datetime.now()})
        return True

    @api.multi
    def button_close(self):
        return True
