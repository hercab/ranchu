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

    workplace_id = fields.Many2one('ipv.work.place', required=True)

    procurement_group_id = fields.Many2one(
        'procurement.group', 'Procurement Group',
        copy=False)

    picking_ids = fields.One2many('stock.picking', 'ipv_id')
    num_pickings = fields.Integer('# pickings', compute='_compute_picking_ids')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('check', 'Check'),
        ('ready', 'Ready'),
        ('open', 'Open'),
        ('close', 'Close'),
        ('cancel', 'Cancelled'),
    ], string='Status', compute='_compute_state', default='draft',
        copy=False, index=True, readonly=True, store=False, track_visibility='onchange',
        help=" * Draft: No ha sido confirmado.\n"
             " * Ready: Chequeado disponibilidad y reservada las cantidades, listo para ser abierto.\n"
             " * Open: Las cantidades han sido movidas, no hay retorno, se puede agregar mas cantidades y productos.\n"
             " * Close: Esta bloqueado y no se puede editar mas.\n")

    ipv_lines = fields.One2many('stock.ipv.line', 'ipv_id')

    raw_lines = fields.One2many('stock.ipv.line', 'ipv_id', string="Pure Raw",
                                domain=[('is_raw', '=', True)], readonly=True)

    saleable_lines = fields.One2many('stock.ipv.line', 'ipv_id', string="Saleable Products",
                                     domain=[('saleable_in_pos', '=', True), ('is_raw', '=', False)])

    show_check_availability = fields.Boolean(compute='_compute_show_check_availability')
    show_validate = fields.Boolean(compute='_compute_show_validate')
    show_open = fields.Boolean(compute='_compute_show_open', help='Compute whether the Open button should be shown.')
    is_locked = fields.Boolean(default=True, help='When the picking is not done this allows changing the '
                                                  'initial demand. When the picking is done this allows '
                                                  'changing the done quantities.')

    date_open = fields.Datetime('Open date', copy=False, readonly=True,
                                help="Date at which the turn has been processed or cancelled.")
    date_close = fields.Datetime('Close date', copy=False, readonly=True,
                                 help="Date at which the turn was closed.")

    @api.depends('procurement_group_id')
    def _compute_picking_ids(self):
        for ipv in self:
            # Clean here because can be pick without move
            self.picking_ids.filtered(lambda p: not p.move_lines).unlink()
            ipv.num_pickings = len(ipv.picking_ids)

    def action_view_ipv_pickings(self):
        self.ensure_one()
        action = self.env.ref('stock.action_picking_tree_all').read()[0]
        pickings = self.picking_ids
        if len(pickings) > 1:
            action['domain'] = [('ipv_id', '=', self.id)]
        elif pickings:
            action['views'] = [(self.env.ref('stock.view_picking_form').id, 'form')]
            action['res_id'] = pickings.id
        return action

    @api.onchange('workplace_id')
    def _compute_child_lines(self):
        last_ipv = self.search([('workplace_id', '=', self.workplace_id.id), ('state', '=', 'close')], limit=1)
        list = []
        for ipvl in last_ipv.saleable_lines.filtered(lambda ipvl: ipvl.on_hand_qty != 0.0):
            data = {
                'product_id': ipvl.product_id.id,
                'bom_id': ipvl.bom_id.id,
            }
            list.append((0, 0, data))
        self.saleable_lines = list

    @api.depends('picking_ids.state')
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

        for ipv in self:
            if ipv.state in ['open', 'close']:
                return
            if not ipv.saleable_lines:
                ipv.state = 'draft'
            elif all(pick.state == 'draft' for pick in ipv.picking_ids):
                ipv.state = 'draft'
            elif all(pick.state in ['assigned', 'done', 'draft'] for pick in ipv.picking_ids):
                ipv.state = 'ready'
            elif all(pick.state == 'cancel' for pick in ipv.picking_ids):
                ipv.state = 'cancel'
            else:
                ipv.state = 'check'

    @api.depends('saleable_lines.request_qty', 'picking_ids.show_check_availability', 'state')
    def _compute_show_check_availability(self):
        self.ensure_one()

        pick_check_availability = any(pick.show_check_availability for pick in self.picking_ids)

        has_qty_to_reserve = any(float_compare(ipvl.request_qty, 0, precision_rounding=ipvl.product_uom.rounding)
                                 and ipvl.state not in ['assigned', 'done', 'cancel'] for ipvl in self.saleable_lines)
        self.show_check_availability = has_qty_to_reserve or pick_check_availability

    @api.depends('picking_ids.show_validate')
    def _compute_show_validate(self):
        self.ensure_one()
        self.show_validate = any(pick.show_validate for pick in self.picking_ids)

    @api.multi
    @api.depends('state', 'is_locked')
    def _compute_show_open(self):
        self.ensure_one()
        self.show_open = self.is_locked and (self.state in ['ready'])

    @api.model
    def create(self, vals):
        if vals.get('name', 'New') == 'New':
            vals['name'] = self.env['ir.sequence'].next_by_code('stock.ipv.seq')
        if not vals.get('procurement_group_id'):
            vals['procurement_group_id'] = self.env["procurement.group"].create({'name': vals['name'],
                                                                                 'move_type': 'one'}).id
        res = super().create(vals)
        return res

    def unlink(self):
        for ipv in self:
            if ipv.state not in ['draft', 'cancel']:
                raise UserError('No puede borrar un IPV que no este cancelado.')
            return super(StockIpv, self).unlink()

    @api.one
    def action_assign(self):
        ipvl_todo = self.ipv_lines.filtered(lambda i: not i.has_moves and not i.is_manufactured)
        moves = self._generate_moves(ipvl_todo)
        # Reservar solo las movidas que vienen del almacen (MP y Merca)
        self.picking_ids |= moves.mapped('picking_id')
        self.picking_ids.filtered(lambda p: p.state not in ['done']).action_assign()
        return True

    def _generate_moves(self, list_ipvl):
        workplace = self.workplace_id
        stock = workplace.stock_loc
        sales = workplace.sales_loc
        # Solo crear movidas para los que se solicita una cantidad
        for ipvl in list_ipvl.filtered('request_qty'):
            elaboration = ipvl.elaboration_loc or workplace.elaboration_loc
            data = {
                # 'sequence': bom_line.sequence,
                'name': self.name,
                # 'bom_line_id': bom_line.id,
                'picking_type_id': self.env.ref('stock_ipv.ipv_picking_type').id,
                'product_id': ipvl.product_id.id,
                'product_uom_qty': ipvl.request_qty,
                'product_uom': ipvl.product_uom.id,
                'location_id': elaboration.id if ipvl.is_manufactured else stock.id,
                'location_dest_id': elaboration.id if ipvl.is_raw else sales.id,
                # 'procure_method': 'make_to_stock',
                'origin': self.name,
                'warehouse_id': stock.get_warehouse().id,
                'group_id': self.procurement_group_id.id,
            }
            ipvl.move_ids = self.env['stock.move'].create(data)
        moves = list_ipvl.mapped('move_ids')._action_confirm()
        return moves

    @api.one
    def action_validate(self):
        for ipvl in self.ipv_lines:
            ipvl.initial_stock_qty = ipvl.on_hand_qty
        # Generate moves for manufactured products
        manufactured_moves = self._generate_moves(self.ipv_lines.filtered('is_manufactured'))
        self.picking_ids |= manufactured_moves.mapped('picking_id')
        for pick in self.picking_ids:
            picking_type = pick.picking_type_id
            precision_digits = self.env['decimal.precision'].precision_get('Product Unit of Measure')
            no_quantities_done = all(float_is_zero(move_line.qty_done, precision_digits=precision_digits)
                                     for move_line in pick.move_line_ids.filtered(lambda m: m.state not in ('done', 'cancel')))
            no_reserved_quantities = all(float_is_zero(move_line.product_qty, precision_rounding=move_line.product_uom_id.rounding)
                                         for move_line in pick.move_line_ids)

            if no_quantities_done:
                for move in pick.move_lines:
                    qty = move.product_uom_qty
                    move._set_quantity_done(qty)

        self.picking_ids.action_done()

    @api.one
    def button_open(self):
        self.action_validate()
        if all(pick.state == 'done' for pick in self.picking_ids):
            self.write({
                'state': 'open',
                'date_open': fields.Datetime.now()})
        return True

    @api.one
    def button_close(self):
        self.write({'state': 'close', 'date_close': fields.Datetime.now()})
        return True

    @api.one
    def action_cancel(self):
        self.picking_ids.action_cancel()
        return True
